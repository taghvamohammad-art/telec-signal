"""Global lifecycle of every signal, tracked in R (1R = distance from fill to stop).

new -> active (filled at the next 15m open) -> closed (tp2 | stop | breakeven)
new -> void (price already past the stop or TP1 at the fill)

Same trade rules as paper trading (TP1 closes half and moves the stop to
entry; stop wins when a candle touches both). Each state change writes a
signal_events row for the Telegram notifier. result_r is the system's own
track record, independent of any account's position limits.
"""

from __future__ import annotations

import json
import logging

from app.core.timeframes import tf_ms
from app.data.store import Store, dt_to_ms, ms_to_dt
from app.paper.sim import Trade

log = logging.getLogger("telec.lifecycle")
M15 = tf_ms("15m")
FEE = 0.0006


def trade_in_r(side: int, fill: float, stop: float, tp1: float, tp2: float, stop_now: float | None, tp1_hit: bool, realized_r: float) -> Trade:
    risk = abs(fill - stop)
    t = Trade(side, fill, 1.0 / risk, stop, tp1, tp2, FEE)
    if tp1_hit:
        t.tp1_hit = True
        t.qty_open = t.qty * 0.5
    if stop_now is not None:
        t.stop = stop_now
    t.realized = realized_r
    return t


class LifecycleTracker:
    def __init__(self, store: Store):
        self.s = store

    async def _event(self, con, cand_id: int, kind: str, payload: dict) -> None:
        await con.execute(
            "INSERT INTO signal_events (candidate_id, kind, payload) VALUES ($1,$2,$3::jsonb) ON CONFLICT DO NOTHING",
            cand_id, kind, json.dumps(payload),
        )

    async def tick(self) -> dict:
        out = {"activated": 0, "closed": 0, "void": 0}
        async with self.s.pool.acquire() as con:
            # 1. fill new signals at the next bar's open
            for c in await con.fetch("SELECT * FROM signal_candidates WHERE status='new' ORDER BY bar_time"):
                fill_ms = dt_to_ms(c["bar_time"]) + M15
                bar = await con.fetchrow("SELECT open FROM candles WHERE symbol=$1 AND tf='15m' AND open_time=$2",
                                         c["symbol"], ms_to_dt(fill_ms))
                if bar is None:
                    continue
                fill, side = bar["open"], c["side"]
                if side * (fill - c["stop"]) <= 0 or side * (c["tp1"] - fill) <= 0:
                    await con.execute("UPDATE signal_candidates SET status='void', fill_price=$2, closed_at=$3, result_r=0 WHERE id=$1",
                                      c["id"], fill, ms_to_dt(fill_ms))
                    await self._event(con, c["id"], "void", {"fill": fill})
                    out["void"] += 1
                    continue
                await con.execute(
                    "UPDATE signal_candidates SET status='active', fill_price=$2, stop_now=stop, last_bar=$3, result_r=$4 WHERE id=$1",
                    c["id"], fill, ms_to_dt(fill_ms - M15), -FEE * fill / abs(fill - c["stop"]))
                out["activated"] += 1

            # 2. walk active signals forward
            for c in await con.fetch("SELECT * FROM signal_candidates WHERE status='active'"):
                t = trade_in_r(c["side"], c["fill_price"], c["stop"], c["tp1"], c["tp2"], c["stop_now"], c["tp1_hit"], c["result_r"] or 0.0)
                last = dt_to_ms(c["last_bar"])
                bars = await con.fetch(
                    "SELECT open_time, high, low FROM candles WHERE symbol=$1 AND tf='15m' AND open_time > $2 ORDER BY open_time LIMIT 3000",
                    c["symbol"], c["last_bar"])
                was_tp1 = t.tp1_hit
                closed_at = None
                for b in bars:
                    o = dt_to_ms(b["open_time"])
                    t.on_candle(o, b["high"], b["low"])
                    last = o
                    if t.tp1_hit and not was_tp1:
                        await self._event(con, c["id"], "tp1", {"r": round(t.realized, 2), "at": o + M15})
                        was_tp1 = True
                    if t.closed:
                        closed_at = o + M15
                        break
                if last == dt_to_ms(c["last_bar"]):
                    continue
                await con.execute(
                    """UPDATE signal_candidates SET stop_now=$2, tp1_hit=$3, last_bar=$4, result_r=$5,
                         status=$6, exit_reason=$7, closed_at=$8 WHERE id=$1""",
                    c["id"], t.stop, t.tp1_hit, ms_to_dt(last), round(t.realized, 4),
                    "closed" if t.closed else "active", t.exit_reason, ms_to_dt(closed_at) if closed_at else None)
                if t.closed:
                    await self._event(con, c["id"], t.exit_reason, {"r": round(t.realized, 2), "at": closed_at})
                    out["closed"] += 1
        return out
