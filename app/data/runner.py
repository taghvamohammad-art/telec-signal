"""Data engine service: universe refresh, backfill, live stream, funding/OI polling, health checks.

Run with:  python -m app.data.runner
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone

from app.alerts import AdminAlerter
from app.config import Settings, load_settings
from app.core.models import Candle
from app.core.timeframes import tf_ms
from app.data.backfill import sync_symbol_tf
from app.data.binance import BinanceFutures, RegionBlocked
from app.data.candles import clean_batch
from app.data.lbank import LBankFutures
from app.data.store import Store
from app.data.stream import KlineStream
from app.data.universe import binance_candidates, build_universe, lbank_index, select_analysis_set

log = logging.getLogger("telec.data")


def now_ms() -> int:
    return int(time.time() * 1000)


class DataEngine:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.store = Store(cfg.database_url)
        self.binance = BinanceFutures(cfg.binance_rest)
        self.lbank: LBankFutures | None = None
        self.alerts = AdminAlerter(cfg.admin_bot_token, cfg.admin_chat_id)
        self.analysis: list[str] = []
        self.stream: KlineStream | None = None
        self._stream_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.on_started = lambda: None

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        await self.store.connect()
        await self.store.apply_schema()
        self.lbank = LBankFutures()
        await self.refresh_universe()
        await self.backfill_all()
        self._restart_stream()
        await self.alerts.send("startup", f"موتور داده شروع شد: {len(self.analysis)} ارز، تایم‌فریم‌ها {','.join(self.cfg.timeframes)}", force=True)

    async def shutdown(self) -> None:
        if self.stream:
            self.stream.stop()
        if self._stream_task:
            await asyncio.gather(self._stream_task, return_exceptions=True)
        await self.binance.close()
        if self.lbank:
            await self.lbank.close()
        await self.store.close()

    # ---- universe ------------------------------------------------------
    async def refresh_universe(self) -> bool:
        """Returns True when the analysis set changed."""
        info, tickers = await asyncio.gather(self.binance.exchange_info(), self.binance.tickers_24h())
        lb_markets = await self.lbank.swap_markets() if self.lbank else {}
        universe = build_universe(
            binance_candidates(info, tickers), lbank_index(lb_markets), self.cfg.universe_size, self.cfg.require_lbank
        )
        if not universe:
            await self.alerts.send("universe_empty", "فهرست ارزها خالی شد؛ اتصال بایننس/ال‌بانک را بررسی کنید")
            return False
        analysis = select_analysis_set(universe, self.cfg.analysis_size, self.cfg.always_include)
        await self.store.save_universe(datetime.now(timezone.utc).date(), universe, analysis)
        changed = set(analysis) != set(self.analysis)
        self.analysis = analysis
        log.info("universe: %d symbols, analysis set: %d (changed=%s)", len(universe), len(analysis), changed)
        return changed

    # ---- candles -------------------------------------------------------
    async def backfill_all(self) -> None:
        t0 = time.time()
        sem = asyncio.Semaphore(4)
        total, gaps = 0, 0

        async def one(sym: str, tf: str) -> None:
            nonlocal total, gaps
            async with sem:
                try:
                    r = await sync_symbol_tf(self.binance, self.store, sym, tf, now_ms(), self.cfg.backfill_days)
                    total += r.written
                    gaps += len(r.gaps_left)
                except RegionBlocked:
                    raise
                except Exception as exc:
                    log.exception("backfill %s %s failed", sym, tf)
                    await self.store.log_issue("backfill_failed", sym, tf, {"error": str(exc)})

        await asyncio.gather(*(one(s, tf) for s in self.analysis for tf in self.cfg.timeframes))
        log.info("backfill done: %d candles written, %d unfilled gaps, %.0fs", total, gaps, time.time() - t0)
        if gaps:
            await self.alerts.send("gaps", f"{gaps} شکاف داده پر نشد (جزئیات در جدول data_issues)")

    async def _persist_stream(self, batch: list[Candle]) -> None:
        accepted, rejected = clean_batch(batch, now_ms())
        await self.store.upsert_candles(accepted)
        for c, problems in rejected:
            await self.store.log_issue("rejected_stream_candle", c.symbol, c.tf, {"open_time": c.open_time, "problems": problems})

    def _restart_stream(self) -> None:
        if self.stream:
            self.stream.stop()
        self.stream = KlineStream(
            self.cfg.binance_ws,
            self.analysis,
            self.cfg.timeframes,
            on_closed=self._persist_stream,
            on_reconnect=self.backfill_all,
            streams_per_conn=self.cfg.ws_streams_per_conn,
        )
        self._stream_task = asyncio.create_task(self.stream.run())

    # ---- derivatives ---------------------------------------------------
    async def poll_derivatives(self) -> None:
        ts = now_ms()
        wanted = set(self.analysis)
        prem = await self.binance.premium_index()
        await self.store.save_funding(
            [(p["symbol"], int(p.get("time", ts)), float(p["lastFundingRate"]), float(p["markPrice"]))
             for p in prem if p.get("symbol") in wanted and p.get("lastFundingRate") not in (None, "")]
        )
        oi_rows = []
        for sym in self.analysis:
            try:
                oi = await self.binance.open_interest(sym)
                oi_rows.append((sym, int(oi.get("time", ts)), float(oi["openInterest"])))
            except Exception as exc:
                log.debug("oi %s: %s", sym, exc)
        await self.store.save_open_interest(oi_rows)

    # ---- periodic jobs -------------------------------------------------
    async def _every(self, seconds: int, job, name: str) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=seconds)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await job()
            except RegionBlocked as exc:
                await self.alerts.send("region", str(exc))
            except Exception as exc:
                log.exception("job %s failed", name)
                await self.alerts.send(f"job:{name}", f"خطا در {name}: {exc}")

    async def _daily_universe(self) -> None:
        if await self.refresh_universe():
            await self.backfill_all()
            self._restart_stream()

    async def _health(self) -> None:
        if not self.stream:
            return
        silent = time.time() - self.stream.last_message_at
        if silent > 180:
            await self.alerts.send("ws_silent", f"{silent:.0f} ثانیه است از وب‌سوکت بایننس پیامی نرسیده")
        smallest = min(tf_ms(t) for t in self.cfg.timeframes) / 1000
        last = await self.store.latest_received()
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > 2.5 * smallest:
            await self.alerts.send("no_candles", "کندل جدیدی در دیتابیس ثبت نشده است")

    async def run_forever(self) -> None:
        await self.start()
        self.on_started()
        jobs = [
            asyncio.create_task(self._every(3600, self.backfill_all, "hourly_backfill")),
            asyncio.create_task(self._every(24 * 3600, self._daily_universe, "daily_universe")),
            asyncio.create_task(self._every(300, self.poll_derivatives, "derivatives")),
            asyncio.create_task(self._every(120, self._health, "health")),
        ]
        await self._stop.wait()
        for j in jobs:
            j.cancel()
        await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()


async def main() -> None:
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = DataEngine(cfg)
    task = asyncio.create_task(engine.run_forever())
    started = asyncio.Event()

    def on_signal() -> None:
        # Once running, stop cleanly; during startup (DB retries, backfill) cancel at once.
        engine.request_stop()
        if not started.is_set():
            task.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)
    engine.on_started = started.set
    try:
        await task
    except asyncio.CancelledError:
        log.info("stopped during startup")
        await engine.shutdown()
    except RegionBlocked as exc:
        log.critical("%s", exc)
        await engine.alerts.send("region", str(exc), force=True)
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
