"""Database queries for the web panel. Returns plain dicts with epoch-ms times."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.data.store import Store, dt_to_ms, ms_to_dt
from app.paper.sim import stats

SESSION_DAYS = 14
ELEVATE_MIN = 15


def token_key(token: str) -> str:
    """Sessions are stored as a hash, so a database leak does not hand out logins."""
    return hashlib.sha256(token.encode()).hexdigest()


def _ms(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def _row(r) -> dict:
    d = {}
    for k, v in dict(r).items():
        if isinstance(v, datetime):
            v = dt_to_ms(v)
        elif k in ("styles", "notes", "events", "votes") and isinstance(v, str):
            v = json.loads(v)
        d[k] = v
    return d


async def _fill_entries(con, book_id: int, day, pos: dict) -> dict:
    """Days saved before entries were tracked: find each position's opening day in the book's history
    and take the daily closes from the candle table."""
    hist = await con.fetch("SELECT day, positions FROM portfolio_days WHERE book_id=$1 AND day <= $2 ORDER BY day DESC LIMIT 400",
                           book_id, day)
    rows = []
    for h in hist:
        p = json.loads(h["positions"]) if isinstance(h["positions"], str) else (h["positions"] or {})
        rows.append((h["day"], {k: (v["n"] if isinstance(v, dict) else v) for k, v in p.items()}))
    since = {}
    for sym, v in pos.items():
        n = v["n"] if isinstance(v, dict) else v
        first = day
        for d, p in rows:
            x = p.get(sym)
            if x is None or (x > 0) != (n > 0):
                break
            first = d
        since[sym] = first
    days = sorted({day, *since.values()})
    closes = {(r["symbol"], r["open_time"]): r["close"] for r in await con.fetch(
        "SELECT symbol, open_time, close FROM candles WHERE tf='1d' AND symbol = ANY($1::text[]) AND open_time = ANY($2::timestamptz[])",
        list(pos), days)}
    out = {}
    for sym, v in pos.items():
        d = dict(v) if isinstance(v, dict) else {"n": v}
        if d.get("entry") is None:
            d["entry"], d["since"] = closes.get((sym, since[sym])), dt_to_ms(since[sym])
        if d.get("px") is None:
            d["px"] = closes.get((sym, day))
        out[sym] = d
    return out


def _position(symbol: str, v, prev) -> dict:
    d = v if isinstance(v, dict) else {"n": v}
    n, entry, px = d.get("n"), d.get("entry"), d.get("px")
    pnl = None
    if entry and px and n:
        pnl = (px / entry - 1) * 100 * (1 if n > 0 else -1)
    return {"symbol": symbol, "notional": n, "prev": prev, "entry": entry, "since": d.get("since"), "price": px, "pnl_pct": pnl,
            "styles": d.get("src") or []}


class WebRepo:
    def __init__(self, store: Store):
        self.s = store

    # ---- auth ---------------------------------------------------------
    async def user_by_name(self, username: str):
        async with self.s.pool.acquire() as con:
            r = await con.fetchrow("SELECT * FROM users WHERE username=$1 AND active", username)
        return dict(r) if r else None

    async def create_session(self, user_id: int, token: str) -> None:
        async with self.s.pool.acquire() as con:
            await con.execute("DELETE FROM sessions WHERE expires_at < now()")
            await con.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES ($1,$2,$3)",
                              token_key(token), user_id, datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS))

    async def user_by_session(self, token: str):
        async with self.s.pool.acquire() as con:
            r = await con.fetchrow(
                """SELECT u.id, u.username, u.is_admin, coalesce(s.elevated_until > now(), false) AS elevated
                   FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token=$1 AND s.expires_at > now() AND u.active""", token_key(token))
        return dict(r) if r else None

    async def delete_session(self, token: str) -> None:
        async with self.s.pool.acquire() as con:
            await con.execute("DELETE FROM sessions WHERE token=$1", token_key(token))

    async def password_hash(self, user_id: int) -> str | None:
        async with self.s.pool.acquire() as con:
            return await con.fetchval("SELECT password_hash FROM users WHERE id=$1 AND active", user_id)

    async def elevate(self, token: str) -> None:
        async with self.s.pool.acquire() as con:
            await con.execute(f"UPDATE sessions SET elevated_until=now() + interval '{ELEVATE_MIN} minutes' WHERE token=$1",
                              token_key(token))

    # ---- live portfolio test ------------------------------------------
    async def portfolio(self) -> list[dict]:
        async with self.s.pool.acquire() as con:
            books = await con.fetch("SELECT id, combo, mode, capital, started_day, last_day, equity FROM portfolio_books ORDER BY combo, mode")
            out = []
            for b in books:
                days = await con.fetch(
                    """SELECT day, equity, price_pnl, funding, fees, dropped, n_long, n_short, gross, net
                       FROM portfolio_days WHERE book_id=$1 ORDER BY day""", b["id"])
                last = await con.fetchrow(
                    "SELECT day, positions FROM portfolio_days WHERE book_id=$1 ORDER BY day DESC LIMIT 1", b["id"])
                prev = await con.fetchrow(
                    "SELECT positions FROM portfolio_days WHERE book_id=$1 ORDER BY day DESC OFFSET 1 LIMIT 1", b["id"])
                curve = [{"t": dt_to_ms(d["day"]), "equity": d["equity"]} for d in days]
                peak, dd = 0.0, 0.0
                for d in days:
                    peak = max(peak, d["equity"])
                    dd = min(dd, d["equity"] / peak - 1)
                load = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # noqa: E731
                pos = load(last["positions"]) if last else {}
                before = load(prev["positions"]) if prev else {}
                size = lambda v: v["n"] if isinstance(v, dict) else v  # noqa: E731  (older days stored a bare number)
                if last and any(not isinstance(v, dict) or v.get("entry") is None or v.get("px") is None for v in pos.values()):
                    pos = await _fill_entries(con, b["id"], last["day"], pos)
                t = days[-1] if days else None
                out.append({"combo": b["combo"], "mode": b["mode"], "capital": b["capital"], "equity": b["equity"],
                            "started_day": b["started_day"], "last_day": b["last_day"], "days": len(days),
                            "return_pct": (b["equity"] / b["capital"] - 1) * 100, "max_dd_pct": dd * 100,
                            "fees": sum(d["fees"] for d in days), "funding": sum(d["funding"] for d in days),
                            "today": dict(t) | {"day": dt_to_ms(t["day"])} if t else None,
                            "curve": curve,
                            "positions": sorted((_position(k, v, size(before[k]) if k in before else None) for k, v in pos.items()),
                                                key=lambda x: -abs(x["notional"])),
                            "closed": sorted(k for k in before if k not in pos)})
        return out

    async def portfolio_backtest(self) -> dict | None:
        async with self.s.pool.acquire() as con:
            r = await con.fetchrow("""SELECT id, created_at, summary, curve FROM backtest_runs
                                      WHERE params->>'kind'='portfolio' ORDER BY id DESC LIMIT 1""")
        if r is None:
            return None
        load = lambda v: json.loads(v) if isinstance(v, str) else v  # noqa: E731
        return {"id": r["id"], "created_at": dt_to_ms(r["created_at"]), "summary": load(r["summary"]), "curves": load(r["curve"])}

    # ---- Hyperliquid whales ------------------------------------------
    async def whales(self) -> dict:
        async with self.s.pool.acquire() as con:
            day = await con.fetchval("SELECT max(day) FROM whale_wallets")
            pts = await con.fetchval("SELECT max(ts) FROM whale_positions")
            fts = await con.fetchval("SELECT max(ts) FROM whale_flow")
            if day is None:
                return {"wallets": [], "flow": [], "day": None}
            rows = await con.fetch(
                """SELECT w.address, w.rank, w.name, w.account_value, w.pnl_all, w.roi_all, w.pnl_month, w.roi_month,
                          w.pnl_week, w.roi_week, w.pnl_day, w.roi_day,
                          count(p.coin) AS n_pos, coalesce(sum(abs(p.notional)), 0) AS gross,
                          coalesce(sum(p.notional), 0) AS net, coalesce(sum(p.upnl), 0) AS upnl,
                          max(p.account_value) AS acct_now
                   FROM whale_wallets w
                   LEFT JOIN whale_positions p ON p.address = w.address AND p.ts = $2
                   WHERE w.day = $1
                   GROUP BY w.address, w.rank, w.name, w.account_value, w.pnl_all, w.roi_all, w.pnl_month, w.roi_month,
                            w.pnl_week, w.roi_week, w.pnl_day, w.roi_day
                   ORDER BY w.rank""", day, pts)
            flow = await con.fetch(
                """SELECT coin, n_long, n_short, long_notional, short_notional, wallets FROM whale_flow
                   WHERE ts = $1 ORDER BY long_notional + short_notional DESC LIMIT 40""", fts) if fts else []
        return {"day": day.isoformat(), "positions_at": dt_to_ms(pts) if pts else None, "flow_at": dt_to_ms(fts) if fts else None,
                "wallets": [_row(r) for r in rows], "flow": [_row(r) for r in flow]}

    async def whale(self, address: str) -> list[dict]:
        async with self.s.pool.acquire() as con:
            pts = await con.fetchval("SELECT max(ts) FROM whale_positions WHERE address=$1", address)
            if pts is None:
                return []
            rows = await con.fetch(
                """SELECT coin, size, notional, entry_px, leverage, upnl FROM whale_positions
                   WHERE address=$1 AND ts=$2 ORDER BY abs(notional) DESC""", address, pts)
        return [_row(r) for r in rows]

    # ---- news ------------------------------------------------------------
    async def news(self, hours: int = 72) -> dict:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT id, source, source_group, url, title, published_at, category, coins, importance, cluster_id
                   FROM news_items WHERE published_at > now() - make_interval(hours => $1) ORDER BY published_at DESC""", hours)
            books = await con.fetch("SELECT positions FROM portfolio_books WHERE mode='exec'")
            events = await con.fetch(
                """SELECT title, country, impact, ts, forecast, previous, actual FROM econ_events
                   WHERE ts > now() - interval '6 hours' AND ts < now() + interval '8 days' ORDER BY ts""")
            sources = await con.fetch(
                """SELECT name, last_ok, last_error, coalesce(last_ok > coalesce(last_error_at, 'epoch'), false) AS ok
                   FROM news_sources ORDER BY name""")
        held = set()
        for b in books:
            pos = json.loads(b["positions"]) if isinstance(b["positions"], str) else (b["positions"] or {})
            for sym in pos:
                base = sym[:-4]
                for p in ("1000000", "1000", "100"):
                    if base.startswith(p) and len(base) > len(p):
                        base = base[len(p):]
                held.add(base)
        stories: dict[int, dict] = {}
        for r in reversed(rows):  # oldest first: the first item of a cluster is the original report
            cid = r["cluster_id"] or r["id"]
            st = stories.get(cid)
            if st is None:
                st = stories[cid] = {"id": cid, "title": r["title"], "url": r["url"], "category": r["category"],
                                     "published": dt_to_ms(r["published_at"]), "importance": r["importance"],
                                     "official": False, "coins": set(), "sources": []}
            st["importance"] = max(st["importance"], r["importance"])
            st["official"] = st["official"] or r["source_group"] == "official"
            st["coins"].update(r["coins"] or [])
            st["latest"] = dt_to_ms(r["published_at"])
            if r["source"] not in [x["name"] for x in st["sources"]]:
                st["sources"].append({"name": r["source"], "url": r["url"]})
        out = []
        for st in stories.values():
            st["coins"] = sorted(st["coins"])
            st["held"] = sorted(set(st["coins"]) & held)
            st["verified"] = len(st["sources"]) >= 2 or st["official"]
            out.append(st)
        out.sort(key=lambda x: x["latest"], reverse=True)
        from app.news.classify import COUNTRY_FA, event_fa

        evs = []
        for e in events:
            d = _row(e)
            d["title_fa"], d["country_fa"] = event_fa(d["title"]), COUNTRY_FA.get(d["country"], d["country"])
            evs.append(d)
        return {"stories": out[:300], "events": evs, "sources": [_row(x) for x in sources]}

    # ---- server operations --------------------------------------------
    async def ops_jobs(self, limit: int = 30) -> list[dict]:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT j.id, j.kind, j.params, j.status, j.exit_code, j.created_at, j.started_at, j.finished_at,
                          u.username AS requested_by, j.requested_ip
                   FROM ops_jobs j LEFT JOIN users u ON u.id=j.requested_by ORDER BY j.id DESC LIMIT $1""", limit)
        out = []
        for r in rows:
            d = _row(r)
            d["params"] = json.loads(d["params"]) if isinstance(d["params"], str) else d["params"]
            out.append(d)
        return out

    async def ops_job(self, job_id: int) -> dict | None:
        async with self.s.pool.acquire() as con:
            r = await con.fetchrow(
                """SELECT j.id, j.kind, j.params, j.status, j.exit_code, j.output, j.created_at, j.started_at,
                          j.finished_at, j.cancel_requested, u.username AS requested_by
                   FROM ops_jobs j LEFT JOIN users u ON u.id=j.requested_by WHERE j.id=$1""", job_id)
        if r is None:
            return None
        d = _row(r)
        d["params"] = json.loads(d["params"]) if isinstance(d["params"], str) else d["params"]
        return d

    async def ops_create(self, kind: str, params: dict, user_id: int, ip: str, max_pending: int) -> int | None:
        async with self.s.pool.acquire() as con:
            async with con.transaction():
                await con.execute("LOCK TABLE ops_jobs IN SHARE ROW EXCLUSIVE MODE")
                pending = await con.fetchval("SELECT count(*) FROM ops_jobs WHERE status IN ('queued','running')")
                if pending >= max_pending:
                    return None
                return await con.fetchval(
                    "INSERT INTO ops_jobs (kind, params, requested_by, requested_ip) VALUES ($1,$2::jsonb,$3,$4) RETURNING id",
                    kind, json.dumps(params), user_id, ip)

    async def ops_runner_alive(self) -> bool:
        async with self.s.pool.acquire() as con:
            v = await con.fetchval("SELECT value FROM app_settings WHERE key='ops_heartbeat'")
        try:
            return v is not None and datetime.now(timezone.utc).timestamp() - float(v) < 30
        except ValueError:
            return False

    async def ops_cancel(self, job_id: int) -> bool:
        async with self.s.pool.acquire() as con:
            r = await con.execute(
                "UPDATE ops_jobs SET cancel_requested=true, updated_at=now() WHERE id=$1 AND status IN ('queued','running')", job_id)
        return r.endswith("1")

    # ---- market -------------------------------------------------------
    async def symbols(self) -> list[dict]:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT u.symbol, u.rank, u.in_analysis, a.regime_1h, a.bias_4h, a.result
                   FROM universe_snapshots u LEFT JOIN analysis_state a ON a.symbol=u.symbol
                   WHERE u.day=(SELECT max(day) FROM universe_snapshots) AND u.in_analysis ORDER BY u.rank""")
        return [dict(r) for r in rows]

    async def candles(self, symbol: str, tf: str, limit: int) -> list[list]:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT open_time, open, high, low, close, volume FROM candles
                   WHERE symbol=$1 AND tf=$2 ORDER BY open_time DESC LIMIT $3""", symbol, tf, limit)
        return [[dt_to_ms(r["open_time"]), r["open"], r["high"], r["low"], r["close"], r["volume"]] for r in reversed(rows)]

    async def signals(self, user_id: int, symbol: str | None, limit: int) -> list[dict]:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT c.id, c.created_at, c.bar_time, c.symbol, c.side, c.score, c.regime_1h, c.bias_4h,
                          c.entry, c.stop, c.tp1, c.tp2, c.risk, c.f_market, c.styles, c.notes,
                          c.lbank_symbol, c.lbank_diff_pct, c.lbank_entry, c.lbank_stop, c.lbank_tp1, c.lbank_tp2,
                          c.status, c.result_r, c.exit_reason, c.tp1_hit, c.closed_at,
                          p.status AS paper_status, p.exit_reason AS paper_exit, p.realized AS paper_pnl,
                          p.skip_reason AS paper_skip, p.closed_at AS paper_closed_at, p.entry AS paper_entry
                   FROM signal_candidates c
                   LEFT JOIN paper_accounts a ON a.user_id=$1
                   LEFT JOIN paper_trades p ON p.candidate_id=c.id AND p.account_id=a.id
                   WHERE ($2::text IS NULL OR c.symbol=$2)
                   ORDER BY c.bar_time DESC LIMIT $3""", user_id, symbol, limit)
        return [_row(r) for r in rows]

    # ---- paper --------------------------------------------------------
    async def paper(self, user_id: int) -> dict:
        async with self.s.pool.acquire() as con:
            acc = await con.fetchrow("SELECT * FROM paper_accounts WHERE user_id=$1", user_id)
            if acc is None:
                return {"account": None}
            trades = [_row(r) for r in await con.fetch(
                "SELECT * FROM paper_trades WHERE account_id=$1 ORDER BY coalesce(opened_at, now()) DESC, id DESC LIMIT 500", acc["id"])]
            marks = {r["symbol"]: r["mark_price"] for r in await con.fetch(
                """SELECT DISTINCT ON (symbol) symbol, mark_price FROM funding_rates
                   WHERE symbol = ANY($1::text[]) ORDER BY symbol, ts DESC""",
                [t["symbol"] for t in trades if t["status"] == "open"])}
        initial = acc["initial_capital"]
        realized = sum(t["realized"] for t in trades if t["status"] != "skipped")
        unreal = 0.0
        for t in trades:
            t.pop("events", None)
            if t["status"] == "open":
                mark = marks.get(t["symbol"])
                t["mark"] = mark
                t["unrealized"] = t["side"] * (mark - t["entry"]) * t["qty_open"] if mark else None
                unreal += t["unrealized"] or 0.0
        closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["closed_at"])
        curve, eq = [{"t": _ms(acc["started_at"]), "equity": initial}], initial
        for t in closed:
            eq += t["realized"]
            curve.append({"t": t["closed_at"], "equity": round(eq, 2)})
        return {
            "account": {"initial_capital": initial, "started_at": _ms(acc["started_at"]), "fee_rate": acc["fee_rate"],
                        "max_open": acc["max_open"], "max_same_side": acc["max_same_side"]},
            "equity_realized": round(initial + realized, 2),
            "equity": round(initial + realized + unreal, 2),
            "unrealized": round(unreal, 2),
            "margin_used": round(sum(t["margin"] or 0 for t in trades if t["status"] == "open"), 2),
            "stats": stats([t["realized"] for t in closed], [p["equity"] for p in curve]),
            "curve": curve,
            "open": [t for t in trades if t["status"] == "open"],
            "history": [t for t in trades if t["status"] != "open"][:300],
        }

    async def paper_reset(self, user_id: int, capital: float) -> None:
        async with self.s.pool.acquire() as con:
            async with con.transaction():
                acc = await con.fetchrow("SELECT id FROM paper_accounts WHERE user_id=$1", user_id)
                if acc:
                    await con.execute("DELETE FROM paper_trades WHERE account_id=$1", acc["id"])
                    await con.execute("UPDATE paper_accounts SET initial_capital=$2, started_at=now() WHERE id=$1", acc["id"], capital)
                else:
                    await con.execute("INSERT INTO paper_accounts (user_id, initial_capital) VALUES ($1,$2)", user_id, capital)

    # ---- backtests ----------------------------------------------------
    async def backtests(self) -> list[dict]:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch("SELECT id, created_at, params, summary FROM backtest_runs WHERE coalesce(params->>'kind', '') NOT IN ('trend', 'intraday', 'crowding', 'lab', 'portfolio') ORDER BY id DESC LIMIT 20")
        out = []
        for r in rows:
            s = json.loads(r["summary"]) if isinstance(r["summary"], str) else r["summary"]
            p = json.loads(r["params"]) if isinstance(r["params"], str) else r["params"]
            out.append({"id": r["id"], "created_at": dt_to_ms(r["created_at"]), "days": p.get("days"),
                        "signals": s.get("signals"), "all": s.get("signal_r", {}).get("all"),
                        "final_equity": s.get("portfolio", {}).get("final_equity"), "capital": s.get("portfolio", {}).get("capital")})
        return out

    async def backtest(self, run_id: int) -> dict | None:
        async with self.s.pool.acquire() as con:
            r = await con.fetchrow("SELECT * FROM backtest_runs WHERE id=$1", run_id)
        if r is None:
            return None
        load = lambda v: json.loads(v) if isinstance(v, str) else v  # noqa: E731
        signals = load(r["signals"])
        return {"id": r["id"], "created_at": dt_to_ms(r["created_at"]), "params": load(r["params"]),
                "summary": load(r["summary"]), "curve": load(r["curve"]),
                "signals": sorted(signals, key=lambda x: x["bar_time"], reverse=True)[:300]}

    # ---- telegram linking ---------------------------------------------
    async def telegram(self, user_id: int) -> dict:
        async with self.s.pool.acquire() as con:
            u = await con.fetchrow("SELECT telegram_chat_id, telegram_name, notify FROM users WHERE id=$1", user_id)
            bot = await con.fetchval("SELECT value FROM app_settings WHERE key='bot_username'")
        return {"linked": u["telegram_chat_id"] is not None, "telegram_name": u["telegram_name"],
                "notify": u["notify"], "bot_username": bot}

    async def telegram_link(self, user_id: int, code: str) -> str | None:
        async with self.s.pool.acquire() as con:
            await con.execute("UPDATE users SET tg_link_code=$2, tg_link_expires=now() + interval '30 minutes' WHERE id=$1",
                              user_id, code)
            bot = await con.fetchval("SELECT value FROM app_settings WHERE key='bot_username'")
        return f"https://t.me/{bot}?start={code}" if bot else None

    async def telegram_unlink(self, user_id: int) -> None:
        async with self.s.pool.acquire() as con:
            await con.execute("UPDATE users SET telegram_chat_id=NULL, telegram_name=NULL WHERE id=$1", user_id)

    # ---- admin-added coins --------------------------------------------
    async def custom_symbols(self) -> list[dict]:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch("SELECT input, symbol, status, note, added_at FROM custom_symbols ORDER BY added_at")
        return [_row(r) for r in rows]

    async def add_custom_symbol(self, user_input: str, user_id: int, limit: int) -> str | None:
        """Returns an error message, or None on success."""
        async with self.s.pool.acquire() as con:
            if await con.fetchval("SELECT 1 FROM custom_symbols WHERE input=$1", user_input):
                return "این ارز قبلاً اضافه شده است."
            if await con.fetchval("SELECT count(*) FROM custom_symbols") >= limit:
                return f"حداکثر {limit} ارز دلخواه؛ برای افزودن، یکی را حذف کنید."
            await con.execute("INSERT INTO custom_symbols (input, added_by) VALUES ($1,$2)", user_input, user_id)
        return None

    async def remove_custom_symbol(self, user_input: str) -> None:
        async with self.s.pool.acquire() as con:
            await con.execute("DELETE FROM custom_symbols WHERE input=$1", user_input)
