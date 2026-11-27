"""Applies the paper-trading rules (app.paper.sim) to stored candidates and candles.

Called by the analysis service after each 15m cycle. Idempotent: it only moves
trades forward from their last processed bar.
"""

from __future__ import annotations

import json
import logging

from app.core.timeframes import tf_ms
from app.data.store import Store, dt_to_ms, ms_to_dt
from app.paper.sim import Trade, funding_times, size_position

log = logging.getLogger("telec.paper")
M15 = tf_ms("15m")


class PaperService:
    def __init__(self, store: Store):
        self.s = store

    # ---- helpers -------------------------------------------------------
    async def _candles(self, con, symbol: str, after_ms: int, limit: int = 2000):
        return await con.fetch(
            """SELECT open_time, open, high, low, close FROM candles
               WHERE symbol=$1 AND tf='15m' AND open_time > $2 ORDER BY open_time LIMIT $3""",
            symbol, ms_to_dt(after_ms), limit,
        )

    async def _funding_rate(self, con, symbol: str, at_ms: int) -> float | None:
        return await con.fetchval(
            "SELECT rate FROM funding_rates WHERE symbol=$1 AND ts <= $2 ORDER BY ts DESC LIMIT 1",
            symbol, ms_to_dt(at_ms + 10 * 60_000),
        )

    @staticmethod
    def _trade_from_row(r) -> Trade:
        t = Trade(side=r["side"], entry=r["entry"], qty=r["qty"], stop=r["stop"], tp1=r["tp1"], tp2=r["tp2"],
                  fee_rate=0.0, qty_open=r["qty_open"], tp1_hit=r["tp1_hit"], realized=r["realized"],
                  fees=r["fees"], funding=r["funding"])
        return t

    # ---- progress open trades -----------------------------------------
    async def _advance(self, con, row, fee_rate: float) -> None:
        t = self._trade_from_row(row)
        t.fee_rate = fee_rate
        last_ms = dt_to_ms(row["last_bar"])
        new_events: list[dict] = []
        before = len(t.events)
        closed_at = None
        for c in await self._candles(con, row["symbol"], last_ms):
            o = dt_to_ms(c["open_time"])
            # funding settled between the previous bar's close and this bar's close
            for ft in funding_times(last_ms + M15, o + M15):
                rate = await self._funding_rate(con, row["symbol"], ft)
                if rate:
                    t.apply_funding(ft, rate, c["close"])
            t.on_candle(o, c["high"], c["low"])
            last_ms = o
            if t.closed:
                closed_at = o + M15
                break
        new_events = t.events[before:]
        if last_ms == dt_to_ms(row["last_bar"]):
            return
        await con.execute(
            """UPDATE paper_trades SET qty_open=$2, stop=$3, tp1_hit=$4, realized=$5, fees=$6, funding=$7,
                 last_bar=$8, status=$9, closed_at=$10, exit_reason=$11, events = events || $12::jsonb
               WHERE id=$1""",
            row["id"], t.qty_open, t.stop, t.tp1_hit, t.realized, t.fees, t.funding, ms_to_dt(last_ms),
            "closed" if t.closed else "open", ms_to_dt(closed_at) if closed_at else None, t.exit_reason,
            json.dumps(new_events),
        )

    # ---- open new trades ----------------------------------------------
    async def _open_new(self, con, acc) -> int:
        opened = 0
        cands = await con.fetch(
            """SELECT c.* FROM signal_candidates c
               WHERE c.created_at >= $2
                 AND NOT EXISTS (SELECT 1 FROM paper_trades p WHERE p.account_id=$1 AND p.candidate_id=c.id)
               ORDER BY c.bar_time""",
            acc["id"], acc["started_at"],
        )
        for c in cands:
            fill_ms = dt_to_ms(c["bar_time"]) + M15
            bar = await con.fetchrow(
                "SELECT open FROM candles WHERE symbol=$1 AND tf='15m' AND open_time=$2", c["symbol"], ms_to_dt(fill_ms)
            )
            if bar is None:
                continue  # fill bar not closed yet; try next cycle
            side, entry = c["side"], bar["open"]
            base = dict(account_id=acc["id"], candidate_id=c["id"], symbol=c["symbol"], side=side, score=c["score"],
                        signal_entry=c["entry"], stop=c["stop"], tp1=c["tp1"], tp2=c["tp2"])

            open_rows = await con.fetch("SELECT symbol, side, margin FROM paper_trades WHERE account_id=$1 AND status='open'", acc["id"])
            equity = acc["initial_capital"] + (await con.fetchval(
                "SELECT coalesce(sum(realized),0) FROM paper_trades WHERE account_id=$1 AND status<>'skipped'", acc["id"]))
            used = sum(r["margin"] or 0 for r in open_rows)
            reason = None
            if any(r["symbol"] == c["symbol"] for r in open_rows):
                reason = "پوزیشن باز روی همین ارز وجود دارد"
            elif len(open_rows) >= acc["max_open"]:
                reason = f"سقف {acc['max_open']} پوزیشن باز پر است"
            elif sum(1 for r in open_rows if r["side"] == side) >= acc["max_same_side"]:
                reason = f"سقف {acc['max_same_side']} پوزیشن هم‌جهت پر است"
            elif side * (entry - c["stop"]) <= 0 or side * (c["tp1"] - entry) <= 0:
                reason = "قیمت ورود از حد ضرر یا TP1 عبور کرده بود"
            sizing = None
            if reason is None:
                sizing = size_position(equity, equity - used, acc["max_open"] - len(open_rows), c["score"], entry, c["stop"], acc["fee_rate"])
                if sizing is None:
                    reason = "مارجین کافی نیست"
            if reason:
                await self._insert(con, base | dict(status="skipped", skip_reason=reason))
                continue

            t = Trade(side, entry, sizing.qty, c["stop"], c["tp1"], c["tp2"], acc["fee_rate"])
            t.open_fee(fill_ms)
            await self._insert(con, base | dict(
                status="open", entry=entry, qty=sizing.qty, qty_open=sizing.qty, leverage=sizing.leverage,
                margin=sizing.margin, risk_pct=sizing.risk_pct, risk_amount=sizing.risk_amount,
                realized=t.realized, fees=t.fees, opened_at=ms_to_dt(fill_ms),
                last_bar=ms_to_dt(fill_ms - M15), events=json.dumps(t.events), note=sizing.note or None,
            ))
            opened += 1
        return opened

    async def _insert(self, con, d: dict) -> None:
        d.setdefault("stop_initial", d["stop"])
        cols = list(d)
        vals = [d[k] for k in cols]
        ph = ",".join(f"${i + 1}" + ("::jsonb" if k == "events" else "") for i, k in enumerate(cols))
        await con.execute(f"INSERT INTO paper_trades ({','.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING", *vals)

    # ---- public -------------------------------------------------------
    async def tick(self) -> dict:
        summary = {"opened": 0, "advanced": 0}
        async with self.s.pool.acquire() as con:
            accounts = await con.fetch("SELECT * FROM paper_accounts")
            for acc in accounts:
                async with con.transaction():
                    summary["opened"] += await self._open_new(con, acc)
                    for row in await con.fetch("SELECT * FROM paper_trades WHERE account_id=$1 AND status='open'", acc["id"]):
                        await self._advance(con, row, acc["fee_rate"])
                        summary["advanced"] += 1
        return summary
