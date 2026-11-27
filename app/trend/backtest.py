"""v3 trend backtest: Donchian ensemble on daily candles, several variants side by side.

  .venv/bin/python -m app.trend.backtest                 # all variants, full stored history
  .venv/bin/python -m app.trend.backtest --since 2021-01-01

Needs daily history: scripts/extend_history.py --days 2700 --tf 1d --universe all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pandas as pd

from app.config import load_settings
from app.data.binance import BinanceFutures
from app.data.store import Store
from app.trend.ensemble import Params, run

log = logging.getLogger("telec.trend")
DAY = 86_400_000

VARIANTS = {
    "long_top20": Params(mode="long", top_n=20),
    "long_top50": Params(mode="long", top_n=50),
    "long_top100": Params(mode="long", top_n=100),
    "short_top20": Params(mode="short", top_n=20),
    "both_top20": Params(mode="both", top_n=20),
    "both_top50": Params(mode="both", top_n=50),
}


async def load_daily(store: Store, symbol: str) -> pd.DataFrame:
    async with store.pool.acquire() as con:
        rows = await con.fetch(
            "SELECT open_time, open, high, low, close, quote_volume FROM candles WHERE symbol=$1 AND tf='1d' ORDER BY open_time",
            symbol)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df.index = [int(t.timestamp() * 1000) for t in df.pop("open_time")]
    df = df.astype(float)
    return df.iloc[:-1] if df.index[-1] + DAY > time.time() * 1000 else df  # drop today's unfinished candle


async def load_daily_funding(store: Store, binance: BinanceFutures, symbol: str, start: int, end: int,
                             download: bool) -> pd.Series:
    """Funding paid per day (sum of that day's settlements)."""
    if download:
        try:
            async with store.pool.acquire() as con:
                last = await con.fetchval("SELECT max(ts) FROM funding_rates WHERE symbol=$1", symbol)
                first = await con.fetchval("SELECT min(ts) FROM funding_rates WHERE symbol=$1", symbol)
            ranges = []
            if first is None:
                ranges.append((start, end))
            else:
                f, l_ = int(first.timestamp() * 1000), int(last.timestamp() * 1000)
                if f > start + DAY:
                    ranges.append((start, f - 1))
                if l_ < end - DAY:
                    ranges.append((l_ + 1, end))
            for a, b in ranges:
                hist = await binance.funding_history(symbol, a, b)
                await store.save_funding([(symbol, t, r, m) for t, r, m in hist])
                await asyncio.sleep(0.7)
        except Exception as exc:
            log.warning("funding %s: %s (baseline funding is assumed for missing days)", symbol, exc)
    async with store.pool.acquire() as con:
        rows = await con.fetch("SELECT ts, rate FROM funding_rates WHERE symbol=$1 ORDER BY ts", symbol)
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([r["rate"] for r in rows], index=[int(r["ts"].timestamp() * 1000) // DAY * DAY for r in rows])
    return s.groupby(level=0).sum()


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def print_report(results: dict, n_symbols: int, seconds: float) -> None:
    print(f"\n=== v3 trend backtest — {n_symbols} coins, {seconds:.0f}s ===")
    print(f"{'variant':<13}{'CAGR%':>8}{'vol%':>7}{'Sharpe':>8}{'maxDD%':>8}{'total%':>10}{'inMkt%':>8}{'entries/mo':>11}")
    for name, r in results.items():
        s = r["stats"]
        print(f"{name:<13}{s.get('cagr'):>8}{s.get('vol'):>7}{str(s.get('sharpe')):>8}{s.get('max_dd'):>8}"
              f"{s.get('total'):>10}{s.get('exposure_days_pct'):>8}{r['coin_entries_per_month']:>11}")
    first = next(iter(results.values()))
    b = first["stats"]["btc"]
    print(f"{'BTC hold':<13}{b.get('cagr'):>8}{b.get('vol'):>7}{str(b.get('sharpe')):>8}{b.get('max_dd'):>8}{b.get('total'):>10}")
    print(f"\nperiod: {first['period'][0]} .. {first['period'][1]}")
    years = sorted(set().union(*[r["yearly"].keys() for r in results.values()]))
    print("\nper year (%):")
    print(f"{'variant':<13}" + "".join(f"{y:>8}" for y in years))
    for name, r in results.items():
        print(f"{name:<13}" + "".join(f"{r['yearly'].get(y, '-'):>8}" for y in years))
    print(f"{'BTC hold':<13}" + "".join(f"{first['btc_yearly'].get(y, '-'):>8}" for y in years))
    print("\ncosts per year (% of capital): " + ", ".join(
        f"{n}: fees {r['stats']['cost_drag_pct_per_year']} / funding {r['stats']['funding_drag_pct_per_year']}"
        for n, r in results.items()))
    print("\nnote: universe = coins in today's list (dead coins are missing, which flatters results);"
          " fees 0.1% per unit traded; funding from Binance history.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="report from this date (YYYY-MM-DD); history before it still warms up")
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--cost", type=float, default=0.001, help="fee + slippage per unit of position traded")
    ap.add_argument("--target-vol", type=float, default=0.25)
    ap.add_argument("--no-download", action="store_true", help="use only stored funding history")
    a = ap.parse_args()
    cfg = load_settings()
    logging.basicConfig(level="INFO", format="%(asctime)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    await store.apply_schema()
    binance = BinanceFutures(cfg.binance_rest)
    t0 = time.time()
    try:
        async with store.pool.acquire() as con:
            symbols = [r["symbol"] for r in await con.fetch(
                "SELECT symbol FROM universe_snapshots WHERE day=(SELECT max(day) FROM universe_snapshots) ORDER BY rank")]
        if "BTCUSDT" not in symbols:
            symbols.insert(0, "BTCUSDT")
        daily, funding = {}, {}
        end = int(time.time() * 1000)
        for i, sym in enumerate(symbols, 1):
            df = await load_daily(store, sym)
            if len(df) < 100:
                log.info("skip %s: only %d daily candles", sym, len(df))
                continue
            daily[sym] = df
            funding[sym] = await load_daily_funding(store, binance, sym, int(df.index[0]), end, not a.no_download)
            if i % 10 == 0:
                log.info("loaded %d/%d", i, len(symbols))
        if "BTCUSDT" not in daily:
            raise SystemExit("no BTCUSDT daily history — run scripts/extend_history.py --days 2700 --tf 1d --universe all")
        btc_days = len(daily["BTCUSDT"])
        log.info("%d coins loaded, BTC daily history %d days (from %s)", len(daily), btc_days, _date(int(daily['BTCUSDT'].index[0])))
        since = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) if a.since else None

        results = {}
        for name in a.variants:
            p = replace(VARIANTS[name], cost=a.cost, target_vol=a.target_vol)
            log.info("running %s ...", name)
            r = run(daily, funding, p, start_ms=since)
            results[name] = {"params": asdict(p), "stats": r.stats, "yearly": r.yearly, "btc_yearly": r.btc_yearly,
                             "coin_entries_per_month": r.coin_entries_per_month,
                             "period": (_date(int(r.equity.index[0])), _date(int(r.equity.index[-1]))),
                             "curve": [[int(t), round(float(v), 5)] for t, v in r.equity.iloc[::3].items()]}
        seconds = time.time() - t0
        summary = {"kind": "trend", "version": "v3.0", "coins": len(daily),
                   "variants": {n: {k: v for k, v in r.items() if k != "curve"} for n, r in results.items()},
                   "seconds": round(seconds)}
        async with store.pool.acquire() as con:
            run_id = await con.fetchval(
                "INSERT INTO backtest_runs (params, summary, curve, signals, trades) VALUES ($1::jsonb,$2::jsonb,$3::jsonb,'[]','[]') RETURNING id",
                json.dumps({"kind": "trend", **vars(a)}), json.dumps(summary, default=float),
                json.dumps({n: r["curve"] for n, r in results.items()}))
        print_report(results, len(daily), seconds)
        print(f"\nsaved as backtest #{run_id}")
    finally:
        await binance.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
