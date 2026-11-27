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
                t = days[-1] if days else None
                out.append({"combo": b["combo"], "mode": b["mode"], "capital": b["capital"], "equity": b["equity"],
                            "started_day": b["started_day"], "last_day": b["last_day"], "days": len(days),
                            "return_pct": (b["equity"] / b["capital"] - 1) * 100, "max_dd_pct": dd * 100,
                            "fees": sum(d["fees"] for d in days), "funding": sum(d["funding"] for d in days),
                            "today": dict(t) | {"day": dt_to_ms(t["day"])} if t else None,
                            "curve": curve,
                            "positions": sorted(({"symbol": k, "notional": v, "prev": before.get(k)} for k, v in pos.items()),
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
