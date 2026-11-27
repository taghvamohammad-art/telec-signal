"""Extend stored candle history backwards (for longer backtests).

  .venv/bin/python scripts/extend_history.py --days 400
  .venv/bin/python scripts/extend_history.py --days 2700 --tf 1d --universe all   # daily since 2019 for the trend test
  .venv/bin/python scripts/extend_history.py --days 2200 --tf 15m --symbols BTCUSDT ETHUSDT SOLUSDT

Fetches, for every symbol of today's analysis set plus BTC and every timeframe,
the candles between (now - days) and the oldest stored candle. Safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.core.timeframes import tf_ms  # noqa: E402
from app.data.binance import BinanceFutures  # noqa: E402
from app.data.candles import clean_batch  # noqa: E402
from app.data.store import Store, dt_to_ms  # noqa: E402

DAY = 86_400_000


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--tf", nargs="*", help="only these timeframes (default: all configured)")
    ap.add_argument("--symbols", nargs="*", help="only these symbols, e.g. BTCUSDT ETHUSDT")
    ap.add_argument("--universe", choices=("analysis", "all"), default="analysis",
                    help="analysis = today's analysis set; all = every coin of today's universe snapshot")
    a = ap.parse_args()
    cfg = load_settings()
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    binance = BinanceFutures(cfg.binance_rest)
    now = int(time.time() * 1000)
    target = now - a.days * DAY
    try:
        async with store.pool.acquire() as con:
            symbols = [r["symbol"] for r in await con.fetch(
                "SELECT symbol FROM universe_snapshots WHERE day=(SELECT max(day) FROM universe_snapshots)"
                + (" AND in_analysis" if a.universe == "analysis" else "") + " ORDER BY rank")]
        if a.symbols:
            symbols = [x.upper() for x in a.symbols]
        if "BTCUSDT" not in symbols:
            symbols.insert(0, "BTCUSDT")
        total = 0
        for i, sym in enumerate(symbols, 1):
            for tf in (a.tf or cfg.timeframes):
                async with store.pool.acquire() as con:
                    oldest = await con.fetchval("SELECT min(open_time) FROM candles WHERE symbol=$1 AND tf=$2", sym, tf)
                end = (dt_to_ms(oldest) - tf_ms(tf)) if oldest else now
                if end <= target:
                    continue
                candles = await binance.klines_range(sym, tf, target, end)
                ok, _ = clean_batch(candles, now)
                total += await store.upsert_candles(ok)
            print(f"[{i}/{len(symbols)}] {sym}: done", flush=True)
        print(f"added {total} candles; history now reaches back about {a.days} days (or to each coin's listing)")
    finally:
        await binance.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
