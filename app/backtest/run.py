"""Backtest driver: load history, replay in parallel, simulate, summarize, save.

  .venv/bin/python -m app.backtest.run --days 120 --capital 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from app.analysis.indicators import add_indicators
from app.analysis.scoring import THRESHOLD, VERSION
from app.backtest.portfolio import breakdown, score_bucket, signal_results, simulate_portfolio
from app.backtest.replay import replay_symbol
from app.config import load_settings
from app.data.binance import BinanceFutures
from app.data.store import Store, ms_to_dt

log = logging.getLogger("telec.backtest")
DAY = 86_400_000


def _worker(args) -> tuple[str, list[dict], dict]:
    symbol, raw, btc_raw, funding, start, end = args
    prep = {tf: add_indicators(df) for tf, df in raw.items()}
    btc_prep = {tf: add_indicators(df) for tf, df in btc_raw.items()} if btc_raw and symbol != "BTCUSDT" else None
    cands, counters = replay_symbol(symbol, prep, btc_prep, funding, start, end)
    return symbol, cands, counters


async def load_frames(store: Store, symbol: str) -> dict[str, pd.DataFrame]:
    out = {}
    async with store.pool.acquire() as con:
        for tf in ("15m", "1h", "4h", "1d"):
            rows = await con.fetch(
                "SELECT open_time, open, high, low, close, volume FROM candles WHERE symbol=$1 AND tf=$2 ORDER BY open_time",
                symbol, tf)
            df = pd.DataFrame([dict(r) for r in rows])
            if df.empty:
                return {}
            df.index = [int(t.timestamp() * 1000) for t in df.pop("open_time")]
            out[tf] = df.astype(float)
    return out


async def load_funding(store: Store, binance: BinanceFutures, symbol: str, start: int, end: int) -> pd.Series:
    """Download settled funding history (stored for reuse) and return it as a series."""
    try:
        hist = await binance.funding_history(symbol, start - 2 * DAY, end)
        await store.save_funding([(symbol, t, r, m) for t, r, m in hist])
    except Exception as exc:  # backtest still runs, just without funding
        log.warning("funding history %s: %s", symbol, exc)
    async with store.pool.acquire() as con:
        rows = await con.fetch("SELECT ts, rate FROM funding_rates WHERE symbol=$1 ORDER BY ts", symbol)
    return pd.Series([r["rate"] for r in rows], index=[int(r["ts"].timestamp() * 1000) for r in rows], dtype=float)


def summarize(results: list[dict], split_ms: int, unseen_before_ms: int | None = None) -> dict:
    closed = [r for r in results if r.get("status") == "closed"]

    def block(rs):
        if not rs:
            return {"n": 0}
        rr = [r["r"] for r in rs]
        wins = [x for x in rr if x > 0]
        gl = -sum(x for x in rr if x <= 0)
        return {"n": len(rr), "win_rate": round(100 * len(wins) / len(rr), 1), "avg_r": round(sum(rr) / len(rr), 3),
                "total_r": round(sum(rr), 2), "profit_factor": round(sum(wins) / gl, 2) if gl > 0 else None}

    return {
        "all": block(closed),
        "in_sample": block([r for r in closed if r["bar_time"] < split_ms]),
        "out_of_sample": block([r for r in closed if r["bar_time"] >= split_ms]),
        # period that was NOT looked at when the v2.1 rules were chosen
        "unseen": block([r for r in closed if unseen_before_ms and r["bar_time"] < unseen_before_ms]),
        "unseen_before": unseen_before_ms,
        "by_style": breakdown(results, lambda r: [s["style"] for s in r["styles"] if s.get("role", "anchor") != "info"]),
        "by_side": breakdown(results, lambda r: ["long" if r["side"] > 0 else "short"]),
        "by_regime": breakdown(results, "regime_1h"),
        "by_score": breakdown(results, score_bucket),
        "by_exit": dict(Counter(r["exit_reason"] for r in closed)),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--fee", type=float, default=0.0006)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--split", type=float, default=0.6, help="share of the period used as in-sample")
    ap.add_argument("--seen-until", default="2026-07-30",
                    help="v2.1 rules were chosen from data after this date; earlier signals are reported as 'unseen'")
    ap.add_argument("symbols", nargs="*")
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
            symbols = [s.upper() for s in a.symbols] or [r["symbol"] for r in await con.fetch(
                "SELECT symbol FROM universe_snapshots WHERE day=(SELECT max(day) FROM universe_snapshots) AND in_analysis ORDER BY rank")]
        end = int(time.time() * 1000)
        start = end - a.days * DAY
        btc_raw = await load_frames(store, "BTCUSDT")
        jobs, candles, funding = [], {}, {}
        for sym in symbols:
            raw = btc_raw if sym == "BTCUSDT" else await load_frames(store, sym)
            if not raw:
                log.info("skip %s: no data", sym)
                continue
            funding[sym] = await load_funding(store, binance, sym, start, end)
            candles[sym] = raw["15m"][["open", "high", "low", "close"]]
            jobs.append((sym, raw, btc_raw, funding[sym], start, end))
        log.info("replaying %d symbols on %d workers ...", len(jobs), a.workers)

        all_cands: list[dict] = []
        counters: Counter = Counter()
        with ProcessPoolExecutor(max_workers=a.workers) as pool:
            for sym, cands, cnt in pool.map(_worker, jobs):
                all_cands.extend(cands)
                counters.update(cnt)
                log.info("  %-14s %3d signals", sym, len(cands))
        all_cands.sort(key=lambda c: c["bar_time"])
        first_bar = min((c["bar_time"] for c in all_cands), default=start)
        split_ms = first_bar + int((end - first_bar) * a.split)

        results = signal_results(all_cands, candles, a.fee)
        port = simulate_portfolio(all_cands, candles, funding, capital=a.capital, fee_rate=a.fee)
        summary = {
            "version": VERSION, "threshold": THRESHOLD, "period": {"start": start, "end": end, "split": split_ms},
            "symbols": len(jobs), "signals": len(all_cands), "decisions": dict(counters),
            "signal_r": summarize(results, split_ms, int(datetime.strptime(a.seen_until, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)), "portfolio": {"stats": port["stats"], "final_equity": port["final_equity"],
                                                                    "capital": a.capital},
            "seconds": round(time.time() - t0),
            "caveats": ["universe = today's analysis set (coins that left the top list are missing)",
                        "funding history from Binance; LBank prices not used",
                        "15m candles: when one candle touches stop and target, the stop is assumed"],
        }
        async with store.pool.acquire() as con:
            run_id = await con.fetchval(
                "INSERT INTO backtest_runs (params, summary, curve, signals, trades) VALUES ($1::jsonb,$2::jsonb,$3::jsonb,$4::jsonb,$5::jsonb) RETURNING id",
                json.dumps(vars(a)), json.dumps(summary), json.dumps(port["curve"]),
                json.dumps(results, default=float), json.dumps(port["trades"], default=float))
        print_report(summary, run_id)
    finally:
        await binance.close()
        await store.close()


def print_report(s: dict, run_id: int) -> None:
    sr, p = s["signal_r"], s["portfolio"]
    print(f"\n=== backtest #{run_id}  ({s['symbols']} symbols, {s['signals']} signals, {s['seconds']}s) ===")
    for name in ("all", "in_sample", "out_of_sample", "unseen"):
        b = sr[name]
        print(f"{name:<14} n={b.get('n', 0):<4} win={b.get('win_rate')}%  avgR={b.get('avg_r')}  totalR={b.get('total_r')}  PF={b.get('profit_factor')}")
    print(f"portfolio: {p['capital']} -> {p['final_equity']}  {p['stats']}")
    for key in ("by_style", "by_side", "by_regime", "by_score"):
        print(f"\n{key}:")
        for k, v in sr[key].items():
            print(f"  {k:<18} n={v['n']:<4} win={v['win_rate']}%  avgR={v['avg_r']}  totalR={v['total_r']}")
    print("\nexits:", sr["by_exit"])
    print("decisions:", s["decisions"])
    print("\nnote:", "; ".join(s["caveats"]))


if __name__ == "__main__":
    asyncio.run(main())
