"""Run every style of the strategy lab with the same rules.

  .venv/bin/python -m app.lab.run
Uses stored daily candles + funding of all Binance perpetuals (dead coins included).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone

from app.config import load_settings
from app.data.binance import BinanceFutures
from app.data.store import Store
from app.lab.styles import DISCOVERY_END, STYLES, build_ctx, deflated_sharpe, pnl, summarize
from app.trend.backtest import load_daily, load_daily_funding

PASS_SHARPE, PASS_YEARS, PASS_DSR = 0.8, 60, 0.90


def _ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.001, help="fee + slippage per unit traded")
    ap.add_argument("--download", action="store_true", help="also fetch missing funding history")
    a = ap.parse_args()
    cfg = load_settings()
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    await store.apply_schema()
    binance = BinanceFutures(cfg.binance_rest)
    t0 = time.time()
    try:
        async with store.pool.acquire() as con:
            symbols = [r["symbol"] for r in await con.fetch(
                "SELECT DISTINCT symbol FROM candles WHERE tf='1d' AND open_time < now() - interval '1 day'")]
        daily, funding = {}, {}
        end = int(time.time() * 1000)
        for s in symbols:
            df = await load_daily(store, s)
            if len(df) < 60:
                continue
            daily[s] = df[["close", "quote_volume"]]
            funding[s] = await load_daily_funding(store, binance, s, int(df.index[0]), end, a.download)
        print(f"loaded {len(daily)} coins in {time.time() - t0:.0f}s; building panel ...", flush=True)
        c = build_ctx(daily, funding)
        del daily
        cut = _ms(DISCOVERY_END)

        results = {}
        for key, (label, fn, hedged) in STYLES.items():
            res = pnl(c, fn(c), a.cost, hedged)
            results[key] = {"label": label, "disc": res[res.index < cut], "hold": res[res.index >= cut]}
            print(f"  {key:<14} done", flush=True)

        daily_sr = [r["disc"]["ret"].mean() / r["disc"]["ret"].std() for r in results.values()
                    if len(r["disc"]) > 60 and r["disc"]["ret"].std() > 0]
        sr_var = statistics.pvariance(daily_sr) if len(daily_sr) > 1 else 0.0
        n_trials = len(STYLES)

        print(f"\n=== strategy lab — {n_trials} styles, cost {a.cost * 100:.2f}% per unit traded ===")
        print(f"discovery: start .. {DISCOVERY_END}   |   holdout: after it (shown only for styles that pass)")
        print(f"pass rule: Sharpe >= {PASS_SHARPE}, >= {PASS_YEARS}% of years positive, deflated Sharpe >= {PASS_DSR}\n")
        print(f"{'style':<15}{'Sharpe':>7}{'CAGR%':>7}{'maxDD%':>8}{'yrs+%':>6}{'DSR':>6}{'carry':>7}{'fees':>6}{'BTCcor':>7}  result")
        out = {}
        for key, r in results.items():
            s = summarize(r["disc"], c.btc)
            dsr = deflated_sharpe(r["disc"]["ret"], n_trials, sr_var)
            s["dsr"] = round(dsr, 2) if dsr is not None else None
            passed = (s.get("sharpe") or -9) >= PASS_SHARPE and (s.get("pos_years") or 0) >= PASS_YEARS and (dsr or 0) >= PASS_DSR
            h = summarize(r["hold"], c.btc) if passed else None
            out[key] = {"label": r["label"], "discovery": s, "passed": passed, "holdout": h}
            verdict = (f"PASS -> holdout Sharpe {h.get('sharpe')}, CAGR {h.get('cagr')}%, maxDD {h.get('max_dd')}%"
                       if passed else "-")
            print(f"{key:<15}{str(s.get('sharpe')):>7}{str(s.get('cagr')):>7}{str(s.get('max_dd')):>8}{str(s.get('pos_years')):>6}"
                  f"{str(s.get('dsr')):>6}{str(s.get('carry_pct_yr')):>7}{str(s.get('fees_pct_yr')):>6}{str(s.get('btc_corr')):>7}  {verdict}")
        print("\nper year % (discovery):")
        for key, o in out.items():
            print(f"  {key:<14} {o['discovery'].get('yearly')}")
        passed = [k for k, o in out.items() if o["passed"]]
        print(f"\n{len(passed)} of {n_trials} styles passed: {', '.join(passed) or 'none'}")
        print("note: carry_neutral assumes a perfect spot hedge and a 0.1% spot fee; real basis moves are not modelled.")
        async with store.pool.acquire() as con:
            rid = await con.fetchval(
                "INSERT INTO backtest_runs (params, summary, curve, signals, trades) VALUES ($1::jsonb,$2::jsonb,'[]','[]','[]') RETURNING id",
                json.dumps({"kind": "lab", **vars(a)}), json.dumps({"kind": "lab", "styles": out}, default=float))
        print(f"saved as backtest #{rid}  ({time.time() - t0:.0f}s)")
    finally:
        await binance.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
