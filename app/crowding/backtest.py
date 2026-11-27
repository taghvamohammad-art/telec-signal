"""Crowding (funding) backtest, long and short, several settings side by side.

  .venv/bin/python -m app.crowding.backtest
Uses the daily candles and funding history already downloaded for the trend test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

from app.config import load_settings
from app.crowding.strategy import TSParams, XSParams, cross_sectional, stats, time_series, trades_per_month, yearly
from app.data.binance import BinanceFutures
from app.data.store import Store
from app.trend.backtest import load_daily, load_daily_funding

HEAD = (f"{'':<26}{'CAGR%':>7}{'vol%':>6}{'Sharpe':>7}{'maxDD%':>7}{'price':>7}{'carry':>7}{'fees':>6}"
        f"{'long':>7}{'short':>7}{'BTCcor':>7}{'trd/mo':>7}")


def line(name, s, tpm):
    g = lambda k: s.get(k, "-")  # noqa: E731
    return (f"{name:<26}{g('cagr'):>7}{g('vol'):>6}{str(g('sharpe')):>7}{g('max_dd'):>7}{g('price_pct_yr'):>7}"
            f"{g('carry_pct_yr'):>7}{g('fees_pct_yr'):>6}{g('long_leg_pct_yr'):>7}{g('short_leg_pct_yr'):>7}"
            f"{str(g('btc_corr')):>7}{tpm:>7}")


def halves(ret):
    n = len(ret) // 2
    f = lambda r: round(float(r.mean() / r.std() * 365 ** 0.5), 2) if r.std() > 0 else None  # noqa: E731
    return f(ret.iloc[:n]), f(ret.iloc[n:])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.001)
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--universe", choices=("today", "everything"), default="today",
                    help="today = coins in today's top list (survivorship bias); everything = every coin with "
                         "stored daily history, delisted ones included")
    ap.add_argument("--since", default=None, help="report from this date, e.g. 2022-01-01")
    a = ap.parse_args()
    cfg = load_settings()
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    await store.apply_schema()
    binance = BinanceFutures(cfg.binance_rest)
    t0 = time.time()
    try:
        async with store.pool.acquire() as con:
            if a.universe == "everything":
                symbols = [r["symbol"] for r in await con.fetch(
                    "SELECT DISTINCT symbol FROM candles WHERE tf='1d' AND open_time < now() - interval '1 day'")]
            else:
                symbols = [r["symbol"] for r in await con.fetch(
                    "SELECT symbol FROM universe_snapshots WHERE day=(SELECT max(day) FROM universe_snapshots) ORDER BY rank")]
        daily, funding = {}, {}
        end = int(time.time() * 1000)
        for s in symbols:
            df = await load_daily(store, s)
            if len(df) < 60:
                continue
            daily[s] = df
            funding[s] = await load_daily_funding(store, binance, s, int(df.index[0]), end, not a.no_download)
        if a.since:
            cut = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            # history before the cut still feeds the signals; only results are trimmed
            trim = lambda res: res[res.index >= cut]  # noqa: E731
        else:
            trim = lambda res: res  # noqa: E731
        btc = daily["BTCUSDT"]["close"].pct_change() if "BTCUSDT" in daily else None
        print(f"\n=== crowding (funding) backtest — universe {a.universe}, since {a.since or 'start'}, {len(daily)} coins, cost {a.cost * 100:.2f}% per unit traded ===")
        print("columns price/carry/fees/long/short are % per year; carry = funding received (+) or paid (-)")

        out = {}
        print("\n1) cross-sectional, market neutral (short highest funding, long lowest):\n" + HEAD)
        base_xs = XSParams(top_n=a.top_n, cost=a.cost)
        for window in (3, 7, 14):
            for reb in (1, 7):
                p = replace(base_xs, window=window, rebalance=reb)
                res, w = cross_sectional(daily, funding, p)
                res = trim(res)
                s = stats(res, btc)
                name = f"XS fund{window}d / rebal {reb}d"
                s["halves_sharpe"] = halves(res["ret"])
                out[name] = {"params": asdict(p), "stats": s, "yearly": yearly(res["ret"])}
                print(line(name, s, trades_per_month(w)))
        d = out["XS fund7d / rebal 7d"]
        print("default XS per year %:", d["yearly"], " Sharpe 1st/2nd half:", d["stats"]["halves_sharpe"])

        print("\n2) time-series contrarian (fade a coin's own extreme funding):\n" + HEAD)
        base_ts = TSParams(top_n=a.top_n, cost=a.cost)
        for z in (1.5, 2.0, 3.0):
            for hold in (3, 7, 14):
                p = replace(base_ts, z_in=z, hold=hold)
                res, w = time_series(daily, funding, p)
                res = trim(res)
                s = stats(res, btc)
                name = f"TS z>{z} / hold {hold}d"
                s["halves_sharpe"] = halves(res["ret"])
                out[name] = {"params": asdict(p), "stats": s, "yearly": yearly(res["ret"])}
                print(line(name, s, trades_per_month(w)))
        d = out["TS z>2.0 / hold 7d"]
        print("default TS per year %:", d["yearly"], " Sharpe 1st/2nd half:", d["stats"]["halves_sharpe"])

        summary = {"kind": "crowding", "coins": len(daily), "variants": out, "seconds": round(time.time() - t0)}
        async with store.pool.acquire() as con:
            rid = await con.fetchval(
                "INSERT INTO backtest_runs (params, summary, curve, signals, trades) VALUES ($1::jsonb,$2::jsonb,'[]','[]','[]') RETURNING id",
                json.dumps({"kind": "crowding", **vars(a)}), json.dumps(summary, default=float))
        print(f"\nsaved as backtest #{rid}  ({time.time() - t0:.0f}s)")
    finally:
        await binance.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
