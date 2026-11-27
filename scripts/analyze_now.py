"""Run the analysis right now and print, per symbol, what each style sees.

Usage:
  .venv/bin/python scripts/analyze_now.py            # whole analysis set, summary
  .venv/bin/python scripts/analyze_now.py SOLUSDT    # one symbol, full detail
Nothing is written to the database.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis.analyzer import analyze, btc_context_from_raw  # noqa: E402
from app.analysis.engine import AnalysisRepo  # noqa: E402
from app.analysis.lbank_check import check_on_lbank  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.data.store import Store  # noqa: E402

SIDE = {1: "LONG ", -1: "SHORT"}


async def main() -> None:
    cfg = load_settings()
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    repo = AnalysisRepo(store)
    now = int(time.time() * 1000)
    only = [a.upper() for a in sys.argv[1:]]
    universe = await repo.analysis_universe()
    symbols = only or list(universe)
    lbank = None
    try:
        from app.data.lbank import LBankFutures
        lbank = LBankFutures()
    except Exception:
        pass
    btc = await repo.frames("BTCUSDT")
    btc_ctx = btc_context_from_raw(btc, now)
    print(f"BTC: regime_1h={btc_ctx[0]}, 1h move={btc_ctx[1] if btc_ctx[1] is None else round(btc_ctx[1], 2)} ATR(15m)\n")
    results: Counter = Counter()
    for sym in symbols:
        frames = btc if sym == "BTCUSDT" else await repo.frames(sym)
        cand, d = analyze(sym, frames, now, funding=await repo.funding(sym), btc_ctx=btc_ctx)
        results[d.result.split("_")[0] if d.result.startswith("score") else d.result] += 1
        votes = " ".join(f"{v.style}:{'+' if v.side > 0 else '-'}{v.s:.2f}" for v in d.votes) or "-"
        print(f"{sym:<14} regime={d.regime_1h:<10} bias4h={d.bias:+d}  {d.result:<24} {votes}")
        if cand:
            print(f"   >>> {SIDE[cand.side]} score={cand.score} entry={cand.entry:g} stop={cand.stop:g} "
                  f"tp1={cand.tp1:g} tp2={cand.tp2:g} f_market={cand.f_market} {cand.notes}")
            if lbank:
                u = universe.get(sym, {})
                lb = await check_on_lbank(lbank, u.get("lbank_symbol"), cand.entry, u.get("multiplier") or 1,
                                          cand.side, cand.entry, cand.stop, cand.tp1, cand.tp2)
                print(f"   LBank {lb.symbol}: {lb.reason} diff={lb.diff_pct}% spread={lb.spread_pct}% depth={lb.depth_usdt} "
                      f"entry={lb.entry} stop={lb.stop} tp1={lb.tp1} tp2={lb.tp2}")
        if only:
            for v in d.votes:
                print(f"   - {v.style} ({'core' if v.core else 'filter'}) {SIDE[v.side]} s={v.s} stop={v.stop}  {v.reason}")
    print("\nsummary:", dict(results))
    if lbank:
        await lbank.close()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
