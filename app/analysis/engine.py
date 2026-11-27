"""Analysis service: after every 15m close, analyze the analysis set and store candidates.

Run with:  python -m app.analysis.engine
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import Counter
from dataclasses import asdict

import pandas as pd

from app.alerts import AdminAlerter
from app.analysis.analyzer import analyze, btc_context_from_raw
from app.analysis.lbank_check import LBankResult, check_on_lbank
from app.analysis.scoring import VERSION, Candidate
from app.config import load_settings
from app.core.timeframes import last_closed_open_time, tf_ms
from app.data.store import Store, dt_to_ms, ms_to_dt
from app.paper.service import PaperService
from app.signals.lifecycle import LifecycleTracker

log = logging.getLogger("telec.analysis")

BARS = {"15m": 400, "1h": 400, "4h": 300, "1d": 250}
COOLDOWN_H = 4
SETTLE_S = 25  # wait after the close so the stream has written the bar


class AnalysisRepo:
    def __init__(self, store: Store):
        self.s = store

    async def analysis_set(self) -> list[str]:
        return list(await self.analysis_universe())

    async def analysis_universe(self) -> dict[str, dict]:
        """symbol -> {lbank_symbol, multiplier} for today's analysis set."""
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT symbol, lbank_symbol, multiplier FROM universe_snapshots
                   WHERE day=(SELECT max(day) FROM universe_snapshots) AND in_analysis ORDER BY rank"""
            )
        return {r["symbol"]: {"lbank_symbol": r["lbank_symbol"], "multiplier": r["multiplier"]} for r in rows}

    async def frame(self, symbol: str, tf: str, limit: int) -> pd.DataFrame:
        async with self.s.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT open_time, open, high, low, close, volume FROM candles
                   WHERE symbol=$1 AND tf=$2 ORDER BY open_time DESC LIMIT $3""",
                symbol, tf, limit,
            )
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame([dict(r) for r in reversed(rows)])
        df.index = [dt_to_ms(t) for t in df.pop("open_time")]
        return df.astype(float)

    async def frames(self, symbol: str) -> dict[str, pd.DataFrame]:
        return {tf: await self.frame(symbol, tf, n) for tf, n in BARS.items()}

    async def funding(self, symbol: str) -> float | None:
        async with self.s.pool.acquire() as con:
            return await con.fetchval("SELECT rate FROM funding_rates WHERE symbol=$1 ORDER BY ts DESC LIMIT 1", symbol)

    async def in_cooldown(self, symbol: str) -> bool:
        async with self.s.pool.acquire() as con:
            return bool(await con.fetchval(
                "SELECT 1 FROM signal_candidates WHERE symbol=$1 AND created_at > now() - make_interval(hours => $2)",
                symbol, COOLDOWN_H,
            ))

    async def save_candidate(self, c: Candidate, bar_ms: int, regime: str, bias: int, lb: LBankResult | None = None) -> int | None:
        """Stores the candidate and its 'new' event; returns the id (None if it already existed)."""
        lb = lb or LBankResult(False, "skipped")
        async with self.s.pool.acquire() as con, con.transaction():
            cid = await con.fetchval(
                """INSERT INTO signal_candidates
                   (bar_time, symbol, side, score, regime_1h, bias_4h, entry, stop, tp1, tp2, risk,
                    f_market, f_news, styles, notes, version,
                    lbank_symbol, lbank_price, lbank_diff_pct, lbank_spread_pct, lbank_depth_usdt,
                    lbank_entry, lbank_stop, lbank_tp1, lbank_tp2)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15::jsonb,$16,
                           $17,$18,$19,$20,$21,$22,$23,$24,$25)
                   ON CONFLICT (symbol, bar_time) DO NOTHING RETURNING id""",
                ms_to_dt(bar_ms), c.symbol, c.side, c.score, regime, bias, c.entry, c.stop, c.tp1, c.tp2, c.risk,
                c.f_market, c.f_news, json.dumps([asdict(v) for v in c.votes], ensure_ascii=False),
                json.dumps(c.notes, ensure_ascii=False), VERSION,
                lb.symbol, lb.price, lb.diff_pct, lb.spread_pct, lb.depth_usdt, lb.entry, lb.stop, lb.tp1, lb.tp2,
            )
            if cid is not None:
                await con.execute("INSERT INTO signal_events (candidate_id, kind) VALUES ($1, 'new') ON CONFLICT DO NOTHING", cid)
        return cid

    async def save_state(self, symbol: str, bar_ms: int, regime: str, bias: int, votes, result: str) -> None:
        async with self.s.pool.acquire() as con:
            await con.execute(
                """INSERT INTO analysis_state (symbol, updated_at, bar_time, regime_1h, bias_4h, votes, result)
                   VALUES ($1, now(), $2, $3, $4, $5::jsonb, $6)
                   ON CONFLICT (symbol) DO UPDATE SET updated_at=now(), bar_time=EXCLUDED.bar_time,
                     regime_1h=EXCLUDED.regime_1h, bias_4h=EXCLUDED.bias_4h, votes=EXCLUDED.votes, result=EXCLUDED.result""",
                symbol, ms_to_dt(bar_ms), regime, bias, json.dumps([asdict(v) for v in votes], ensure_ascii=False), result,
            )


async def run_cycle(repo: AnalysisRepo, alerts: AdminAlerter, now_ms: int, lbank=None, signals: bool = True) -> dict:
    expected = last_closed_open_time(now_ms, "15m")
    universe = await repo.analysis_universe()
    symbols = list(universe)
    btc = await repo.frames("BTCUSDT")
    btc_ctx = btc_context_from_raw(btc, now_ms)
    stats: Counter = Counter()
    for sym in symbols:
        frames = btc if sym == "BTCUSDT" else await repo.frames(sym)
        m15 = frames["15m"]
        if m15.empty or int(m15.index[-1]) < expected:
            stats["stale"] += 1
            continue
        cand, diag = analyze(sym, frames, now_ms, funding=await repo.funding(sym), btc_ctx=btc_ctx)
        stats[f"regime:{diag.regime_1h}"] += 1
        await repo.save_state(sym, expected, diag.regime_1h, diag.bias, diag.votes, diag.result)
        if cand is None or not signals:  # signals off: keep market state for the chart, publish nothing
            continue
        if await repo.in_cooldown(sym):
            stats["cooldown"] += 1
            continue
        lb = None
        if lbank is not None:
            u = universe.get(sym, {})
            lb = await check_on_lbank(lbank, u.get("lbank_symbol"), cand.entry, u.get("multiplier") or 1,
                                      cand.side, cand.entry, cand.stop, cand.tp1, cand.tp2)
            if not lb.ok and lb.reason != "lbank_unavailable":
                stats[lb.reason] += 1
                await repo.save_state(sym, expected, diag.regime_1h, diag.bias, diag.votes, lb.reason)
                log.info("REJECTED %s on LBank: %s (diff=%s%% spread=%s%% depth=%s)", sym, lb.reason, lb.diff_pct, lb.spread_pct, lb.depth_usdt)
                continue
            if not lb.ok:
                cand.notes.append("قیمت ال‌بانک در دسترس نبود؛ سطوح بر اساس بایننس است")
        if await repo.save_candidate(cand, expected, diag.regime_1h, diag.bias, lb if lb and lb.ok else None):
            stats["candidates"] += 1
            log.info("CANDIDATE %s %s score=%.1f entry=%g stop=%g styles=%s", sym,
                     "LONG" if cand.side > 0 else "SHORT", cand.score, cand.entry, cand.stop,
                     ",".join(v.style for v in cand.votes))
    if symbols and stats["stale"] > len(symbols) / 2:
        await alerts.send("analysis_stale", f"داده {stats['stale']} از {len(symbols)} ارز به‌روز نیست؛ موتور داده را بررسی کنید")
    return dict(stats)


async def main() -> None:
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(cfg.database_url)
    await store.connect()
    await store.apply_schema()
    repo = AnalysisRepo(store)
    paper = PaperService(store)
    tracker = LifecycleTracker(store)
    # v2.1 signals showed no edge in backtests; off unless SIGNALS_V2=1 in .env
    signals = os.environ.get("SIGNALS_V2", "0") == "1"
    log.info("v2.1 signal publishing is %s", "ON" if signals else "OFF")
    lbank = None
    if signals and os.environ.get("LBANK_CHECK", "1") != "0":
        from app.data.lbank import LBankFutures

        lbank = LBankFutures()
    alerts = AdminAlerter(cfg.admin_bot_token, cfg.admin_chat_id)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    step = tf_ms("15m")
    while not stop.is_set():
        now = int(time.time() * 1000)
        nxt = now - now % step + step + SETTLE_S * 1000
        try:
            await asyncio.wait_for(stop.wait(), timeout=(nxt - now) / 1000)
            break
        except asyncio.TimeoutError:
            pass
        t0 = time.time()
        try:
            stats = await run_cycle(repo, alerts, int(time.time() * 1000), lbank, signals)
            log.info("cycle done in %.1fs: %s", time.time() - t0, stats)
        except Exception as exc:
            log.exception("analysis cycle failed")
            await alerts.send("analysis_error", f"خطا در چرخه تحلیل: {exc}")
        try:
            log.info("lifecycle: %s", await tracker.tick())
        except Exception as exc:
            log.exception("lifecycle tick failed")
            await alerts.send("lifecycle_error", f"خطا در پیگیری سیگنال‌ها: {exc}")
        try:
            log.info("paper: %s", await paper.tick())
        except Exception as exc:
            log.exception("paper tick failed")
            await alerts.send("paper_error", f"خطا در ترید مجازی: {exc}")
    if lbank:
        await lbank.close()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
