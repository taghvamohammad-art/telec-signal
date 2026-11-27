"""Web panel (Starlette). Run: uvicorn app.web.main:app --host 127.0.0.1 --port 8000 --proxy-headers"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

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
        Route("/api/custom-symbols", api_custom_list),
        Route("/api/custom-symbols", api_custom_add, methods=["POST"]),
        Route("/api/custom-symbols/{sym}", api_custom_remove, methods=["DELETE"]),
        Route("/api/telegram", api_telegram),
        Route("/api/telegram/link", api_telegram_link, methods=["POST"]),
        Route("/api/telegram/unlink", api_telegram_unlink, methods=["POST"]),
        Route("/api/backtests/{run_id:int}", api_backtest),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


app = create_app()
