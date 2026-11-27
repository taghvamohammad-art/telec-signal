"""REST backfill: bring each (symbol, timeframe) up to the last closed candle and repair gaps."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.timeframes import last_closed_open_time, tf_ms
from app.data.candles import clean_batch, find_gaps

log = logging.getLogger(__name__)
DAY_MS = 86_400_000


@dataclass
class SyncResult:
    symbol: str
    tf: str
    written: int = 0
    rejected: int = 0
    gaps_left: list[tuple[int, int]] = field(default_factory=list)


async def sync_symbol_tf(client, store, symbol: str, tf: str, now_ms: int, backfill_days: int, verify_days: int = 3) -> SyncResult:
    """client: has klines_range(); store: has last_open_time(), upsert_candles(), open_times(), log_issue()."""
    res = SyncResult(symbol, tf)
    step = tf_ms(tf)
    end = last_closed_open_time(now_ms, tf)
    last = await store.last_open_time(symbol, tf)
    start = last + step if last is not None else end - backfill_days * DAY_MS

    if start <= end:
        fetched = await client.klines_range(symbol, tf, start, end)
        accepted, rejected = clean_batch(fetched, now_ms)
        res.written += await store.upsert_candles(accepted)
        res.rejected += len(rejected)
        for c, problems in rejected[:5]:
            await store.log_issue("rejected_candle", symbol, tf, {"open_time": c.open_time, "problems": problems})

    # Verify the recent window and repair holes once.
    vstart = max(end - verify_days * DAY_MS, end - backfill_days * DAY_MS)
    have = await store.open_times(symbol, tf, vstart, end)
    if not have:
        return res
    vstart = max(vstart, min(have))  # a new listing has no history before its first candle
    gaps = find_gaps(have, tf, vstart, end)
    for g_start, g_end in gaps:
        fetched = await client.klines_range(symbol, tf, g_start, g_end)
        accepted, _ = clean_batch(fetched, now_ms)
        res.written += await store.upsert_candles(accepted)
    if gaps:
        have = await store.open_times(symbol, tf, vstart, end)
        res.gaps_left = find_gaps(have, tf, vstart, end)
        if res.gaps_left:
            # Can be legitimate (exchange maintenance, new listing); recorded, not fatal.
            await store.log_issue("gap_unfilled", symbol, tf, {"gaps": res.gaps_left[:20]})
    return res
