"""Web panel (Starlette). Run: uvicorn app.web.main:app --host 127.0.0.1 --port 8000 --proxy-headers"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.ops.jobs import KINDS, InvalidParams, describe
from app.web.auth import new_token, verify_password

log = logging.getLogger("telec.web")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "telec_session"
TFS = {"15m", "1h", "4h", "1d"}
LOGIN_LIMIT = 5  # failed attempts per IP per 5 minutes

_failed: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _too_many(ip: str) -> bool:
    q, now = _failed[ip], time.time()
    while q and now - q[0] > 300:
        q.popleft()
    return len(q) >= LOGIN_LIMIT


async def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    return await request.app.state.repo.user_by_session(token)


def _unauth() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ---- pages -------------------------------------------------------------
async def index(request: Request) -> Response:
    if await current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


async def login_page(request: Request) -> Response:
    return FileResponse(STATIC / "login.html")


# ---- auth api ----------------------------------------------------------
async def api_login(request: Request) -> Response:
    ip = _client_ip(request)
    if _too_many(ip):
        return JSONResponse({"error": "تعداد تلاش زیاد است؛ ۵ دقیقه بعد دوباره امتحان کنید"}, status_code=429)
    try:
        body = await request.json()
        username, password = str(body["username"]).strip(), str(body["password"])
    except Exception:
        return JSONResponse({"error": "درخواست نامعتبر"}, status_code=400)
    user = await request.app.state.repo.user_by_name(username)
    if not user or not verify_password(password, user["password_hash"]):
        _failed[ip].append(time.time())
        return JSONResponse({"error": "نام کاربری یا رمز اشتباه است"}, status_code=401)
    token = new_token()
    await request.app.state.repo.create_session(user["id"], token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=14 * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    return resp


async def api_logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE)
    if token:
        await request.app.state.repo.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


async def api_me(request: Request) -> Response:
    user = await current_user(request)
    return JSONResponse(user) if user else _unauth()


# ---- data api ----------------------------------------------------------
async def api_symbols(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.symbols())


async def api_candles(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    symbol = request.query_params.get("symbol", "").upper()
    tf = request.query_params.get("tf", "1h")
    try:
        limit = max(10, min(int(request.query_params.get("limit", "500")), 1500))
    except ValueError:
        limit = 500
    if not symbol.isalnum() or tf not in TFS:
        return JSONResponse({"error": "invalid symbol or timeframe"}, status_code=400)
    return JSONResponse(await request.app.state.repo.candles(symbol, tf, limit))


async def api_signals(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return _unauth()
    symbol = request.query_params.get("symbol") or None
    if symbol and not symbol.isalnum():
        return JSONResponse({"error": "invalid symbol"}, status_code=400)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    return JSONResponse(await request.app.state.repo.signals(user["id"], symbol.upper() if symbol else None, limit))


async def api_paper(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.paper(user["id"]))


async def api_paper_reset(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return _unauth()
    try:
        capital = float((await request.json())["capital"])
    except Exception:
        return JSONResponse({"error": "مقدار سرمایه نامعتبر است"}, status_code=400)
    if not 10 <= capital <= 1e9:
        return JSONResponse({"error": "سرمایه باید بین ۱۰ تا یک میلیارد دلار باشد"}, status_code=400)
    await request.app.state.repo.paper_reset(user["id"], capital)
    return JSONResponse({"ok": True})


async def api_portfolio(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.portfolio())


async def api_portfolio_backtest(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.portfolio_backtest())


WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")


async def api_whales(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.whales())


async def api_whale(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    address = request.path_params["address"].lower()
    if not WALLET_RE.match(address):
        return JSONResponse({"error": "آدرس نامعتبر"}, status_code=400)
    return JSONResponse(await request.app.state.repo.whale(address))


async def api_news(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.news())


async def api_backtests(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.backtests())


async def api_backtest(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    run = await request.app.state.repo.backtest(int(request.path_params["run_id"]))
    return JSONResponse(run) if run else JSONResponse({"error": "not found"}, status_code=404)


async def api_telegram(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.telegram(user["id"]))


async def api_telegram_link(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return _unauth()
    link = await request.app.state.repo.telegram_link(user["id"], new_token()[:24].replace("-", "x").replace("_", "y"))
    if not link:
        return JSONResponse({"error": "بات تلگرام هنوز راه‌اندازی نشده است. توکن بات را در تنظیمات سرور وارد کنید."}, status_code=409)
    return JSONResponse({"link": link})


async def api_telegram_unlink(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return _unauth()
    await request.app.state.repo.telegram_unlink(user["id"])
    return JSONResponse({"ok": True})


MAX_CUSTOM = 15


async def _admin(request: Request):
    user = await current_user(request)
    return user if user and user.get("is_admin") else None


async def api_custom_list(request: Request) -> Response:
    if await current_user(request) is None:
        return _unauth()
    return JSONResponse(await request.app.state.repo.custom_symbols())


async def api_custom_add(request: Request) -> Response:
    user = await _admin(request)
    if user is None:
        return JSONResponse({"error": "فقط مدیر می‌تواند ارز اضافه کند."}, status_code=403)
    try:
        raw = str((await request.json())["symbol"])
    except Exception:
        return JSONResponse({"error": "نماد را وارد کنید."}, status_code=400)
    sym = raw.strip().upper().replace("/", "").replace("-", "").replace(" ", "")
    if not sym.isalnum() or not 2 <= len(sym) <= 20:
        return JSONResponse({"error": "نماد فقط حروف و عدد انگلیسی است، مثل WIF یا WIFUSDT."}, status_code=400)
    err = await request.app.state.repo.add_custom_symbol(sym, user["id"], MAX_CUSTOM)
    if err:
        return JSONResponse({"error": err}, status_code=409)
    return JSONResponse({"ok": True})


async def api_custom_remove(request: Request) -> Response:
    if await _admin(request) is None:
        return JSONResponse({"error": "فقط مدیر می‌تواند ارز حذف کند."}, status_code=403)
    await request.app.state.repo.remove_custom_symbol(request.path_params["sym"].upper())
    return JSONResponse({"ok": True})


# ---- server operations (admin + recent password check) ----------------------
MAX_PENDING_JOBS = 3


async def api_ops_kinds(request: Request) -> Response:
    user = await _admin(request)
    if user is None:
        return JSONResponse({"error": "فقط مدیر"}, status_code=403)
    alive = await request.app.state.repo.ops_runner_alive()
    return JSONResponse({"kinds": describe(), "elevated": bool(user.get("elevated")), "runner_alive": alive})


async def api_ops_confirm(request: Request) -> Response:
    user = await _admin(request)
    if user is None:
        return JSONResponse({"error": "فقط مدیر"}, status_code=403)
    ip = _client_ip(request)
    if _too_many(ip):
        return JSONResponse({"error": "تعداد تلاش زیاد است؛ ۵ دقیقه بعد دوباره امتحان کنید"}, status_code=429)
    try:
        password = str((await request.json())["password"])
    except Exception:
        return JSONResponse({"error": "رمز را وارد کنید"}, status_code=400)
    stored = await request.app.state.repo.password_hash(user["id"])
    if not stored or not verify_password(password, stored):
        _failed[ip].append(time.time())
        log.warning("ops confirm failed for %s from %s", user["username"], ip)
        return JSONResponse({"error": "رمز اشتباه است"}, status_code=400)  # not 401: that would log the user out
    await request.app.state.repo.elevate(request.cookies.get(COOKIE))
    return JSONResponse({"ok": True})


async def api_ops_jobs(request: Request) -> Response:
    if await _admin(request) is None:
        return JSONResponse({"error": "فقط مدیر"}, status_code=403)
    return JSONResponse(await request.app.state.repo.ops_jobs())


async def api_ops_job(request: Request) -> Response:
    if await _admin(request) is None:
        return JSONResponse({"error": "فقط مدیر"}, status_code=403)
    job = await request.app.state.repo.ops_job(int(request.path_params["job_id"]))
    return JSONResponse(job) if job else JSONResponse({"error": "not found"}, status_code=404)


async def api_ops_create(request: Request) -> Response:
    user = await _admin(request)
    if user is None:
        return JSONResponse({"error": "فقط مدیر"}, status_code=403)
    if not user.get("elevated"):
        return JSONResponse({"error": "برای اجرای عملیات، رمز خود را دوباره وارد کنید.", "need_confirm": True}, status_code=403)
    try:
        body = await request.json()
        kind = KINDS[str(body["kind"])]
        params = kind.clean(body.get("params") or {})
    except KeyError:
        return JSONResponse({"error": "عملیات ناشناخته"}, status_code=400)
    except InvalidParams as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "درخواست نامعتبر"}, status_code=400)
    ip = _client_ip(request)
    job_id = await request.app.state.repo.ops_create(kind.key, params, user["id"], ip, MAX_PENDING_JOBS)
    if job_id is None:
        return JSONResponse({"error": f"حداکثر {MAX_PENDING_JOBS} عملیات در صف؛ صبر کنید تا یکی تمام شود."}, status_code=409)
    log.warning("ops job %s %s %s queued by %s from %s", job_id, kind.key, params, user["username"], ip)
    return JSONResponse({"id": job_id})


async def api_ops_cancel(request: Request) -> Response:
    user = await _admin(request)
    if user is None:
        return JSONResponse({"error": "فقط مدیر"}, status_code=403)
    if not user.get("elevated"):
        return JSONResponse({"error": "برای لغو، رمز خود را دوباره وارد کنید.", "need_confirm": True}, status_code=403)
    ok = await request.app.state.repo.ops_cancel(int(request.path_params["job_id"]))
    return JSONResponse({"ok": ok})


# ---- hardening ---------------------------------------------------------------
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
       "object-src 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class Hardening:
    """Security headers on every response; changing requests must come from this site (CSRF)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if scope["method"] in UNSAFE and scope["path"].startswith("/api/"):
            host = headers.get("host", "")
            origin = headers.get("origin") or ""
            referer = headers.get("referer") or ""
            src = origin or referer
            ok = bool(src) and src.split("://", 1)[-1].split("/", 1)[0] == host
            if not ok or headers.get("content-type", "").split(";")[0].strip() != "application/json":
                resp = JSONResponse({"error": "درخواست از مبدأ نامعتبر رد شد"}, status_code=403)
                return await resp(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                hs += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                if scope["path"].startswith("/api/"):
                    hs.append((b"cache-control", b"no-store"))
                message["headers"] = hs
            await send(message)

        return await self.app(scope, receive, send_with_headers)


def create_app(repo_factory=None) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        store = None
        if repo_factory is None:
            from app.config import load_settings
            from app.data.store import Store
            from app.web.repo import WebRepo

            cfg = load_settings()
            store = Store(cfg.database_url)
            await store.connect()
            app.state.repo = WebRepo(store)
        else:
            app.state.repo = repo_factory()
        yield
        if store:
            await store.close()

    routes = [
        Route("/", index),
        Route("/login", login_page),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
        Route("/api/me", api_me),
        Route("/api/symbols", api_symbols),
        Route("/api/candles", api_candles),
        Route("/api/signals", api_signals),
        Route("/api/paper", api_paper),
        Route("/api/paper/reset", api_paper_reset, methods=["POST"]),
        Route("/api/backtests", api_backtests),
        Route("/api/portfolio", api_portfolio),
        Route("/api/whales", api_whales),
        Route("/api/news", api_news),
        Route("/api/whales/{address}", api_whale),
        Route("/api/portfolio/backtest", api_portfolio_backtest),
        Route("/api/custom-symbols", api_custom_list),
        Route("/api/custom-symbols", api_custom_add, methods=["POST"]),
        Route("/api/custom-symbols/{sym}", api_custom_remove, methods=["DELETE"]),
        Route("/api/telegram", api_telegram),
        Route("/api/telegram/link", api_telegram_link, methods=["POST"]),
        Route("/api/telegram/unlink", api_telegram_unlink, methods=["POST"]),
        Route("/api/backtests/{run_id:int}", api_backtest),
        Route("/api/ops/kinds", api_ops_kinds),
        Route("/api/ops/confirm", api_ops_confirm, methods=["POST"]),
        Route("/api/ops/jobs", api_ops_jobs),
        Route("/api/ops/jobs", api_ops_create, methods=["POST"]),
        Route("/api/ops/jobs/{job_id:int}", api_ops_job),
        Route("/api/ops/jobs/{job_id:int}/cancel", api_ops_cancel, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    from starlette.middleware import Middleware

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(Hardening)])


app = create_app()
