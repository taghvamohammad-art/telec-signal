"""Intraday momentum backtest (long and short), several coins and settings side by side.

  .venv/bin/python -m app.intraday.backtest
  .venv/bin/python -m app.intraday.backtest BTCUSDT ETHUSDT

Needs 15m history: scripts/extend_history.py --days 2200 --tf 15m --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pandas as pd

from app.config import load_settings
from app.data.store import Store
from app.intraday.noise import Params, portfolio, run_halfhour, run_noise, stats, yearly

DEFAULT = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]


async def load_15m(store: Store, symbol: str) -> pd.DataFrame:
    async with store.pool.acquire() as con:
        rows = await con.fetch(
            "SELECT open_time, open, high, low, close, volume FROM candles WHERE symbol=$1 AND tf='15m' ORDER BY open_time", symbol)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df.index = [int(t.timestamp() * 1000) for t in df.pop("open_time")]
    return df.astype(float)


def _d(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def line(name: str, s: dict) -> str:
    return (f"{name:<22}{s.get('cagr', '-'):>8}{s.get('vol', '-'):>7}{str(s.get('sharpe', '-')):>8}"
            f"{s.get('max_dd', '-'):>8}{s.get('win_days', '-'):>7}{s.get('trades_per_month', '-'):>9}")


HEAD = f"{'':<22}{'CAGR%':>8}{'vol%':>7}{'Sharpe':>8}{'maxDD%':>8}{'win%':>7}{'trd/mo':>9}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--cost", type=float, default=0.0007, help="fee + slippage per side")
    a = ap.parse_args()
    cfg = load_settings()
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    await store.apply_schema()
    t0 = time.time()
    try:
        data = {}
        for s in [x.upper() for x in a.symbols] or DEFAULT:
            df = await load_15m(store, s)
            if len(df) > 60 * 96:
                data[s] = df
            else:
                print(f"skip {s}: only {len(df)} 15m candles")
        base = Params(cost=a.cost)
        print(f"\n=== intraday momentum — {len(data)} coins, cost {a.cost * 100:.2f}% per side ===")
        for s, df in data.items():
            print(f"  {s:<10} {_d(df.index[0])} .. {_d(df.index[-1])}")

        print("\nA) noise-area breakout, default settings, per coin:\n" + HEAD)
        per = {s: run_noise(df, base) for s, df in data.items()}
        for s, r in per.items():
            print(line(s, stats(r["ret"], r["trades"])))
        pr, pt = portfolio(per)
        main_stats = stats(pr, pt)
        print(line("PORTFOLIO", main_stats))
        print("per year %:", yearly(pr))

        print("\nsame strategy, other settings (portfolio) — a real edge should hold in all of them:\n" + HEAD)
        grid = {}
        for mult in (0.75, 1.0, 1.5):
            for step in (2, 4):
                p = replace(base, mult=mult, step=step)
                r_, t_ = portfolio({s: run_noise(df, p) for s, df in data.items()})
                grid[f"mult {mult} / {step * 15}m"] = stats(r_, t_)
                print(line(f"mult {mult} / {step * 15}m", grid[f"mult {mult} / {step * 15}m"]))
        r_, t_ = portfolio({s: run_noise(df, replace(base, cost=0)) for s, df in data.items()})
        grid["no costs"] = stats(r_, t_)
        print(line("default, no costs", grid["no costs"]))

        print("\nB) half-hour momentum (portfolio):\n" + HEAD)
        half = {}
        for sig in ("first", "sofar"):
            r_, t_ = portfolio({s: run_halfhour(df, base, sig) for s, df in data.items()})
            half[sig] = {"stats": stats(r_, t_), "yearly": yearly(r_)}
            print(line(f"{sig} -> last 30m", half[sig]["stats"]))
            print("   per year %:", half[sig]["yearly"])

        bh = pd.concat({s: df["close"].groupby(df.index // 86_400_000).last().pct_change() for s, df in data.items()}, axis=1)
        print("\nhold all coins (equal weight, for reference):", line("", stats(bh.mean(axis=1).fillna(0))).strip())

        summary = {"kind": "intraday", "coins": list(data), "noise": {"stats": main_stats, "yearly": yearly(pr),
                   "per_coin": {s: stats(r["ret"], r["trades"]) for s, r in per.items()}, "grid": grid},
                   "halfhour": half, "params": asdict(base), "seconds": round(time.time() - t0)}
        curve = [[int(t), round(float(v), 5)] for t, v in (1 + pr).cumprod().iloc[::2].items()]
        async with store.pool.acquire() as con:
            rid = await con.fetchval(
                "INSERT INTO backtest_runs (params, summary, curve, signals, trades) VALUES ($1::jsonb,$2::jsonb,$3::jsonb,'[]','[]') RETURNING id",
                json.dumps({"kind": "intraday", **vars(a)}), json.dumps(summary, default=float), json.dumps(curve))
        print(f"\nsaved as backtest #{rid}  ({time.time() - t0:.0f}s)")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
