"""Binance futures kline WebSocket consumer. Only CLOSED candles are passed on."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable

from app.core.models import Candle
from app.data.candles import parse_binance_ws_kline

log = logging.getLogger(__name__)

MAX_CONN_AGE_S = 23 * 3600  # Binance drops connections at 24h; rotate before that


def stream_names(symbols: list[str], tfs: list[str]) -> list[str]:
    return [f"{s.lower()}@kline_{tf}" for s in symbols for tf in tfs]


def chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def combined_url(base_ws: str, streams: list[str]) -> str:
    return f"{base_ws.rstrip('/')}/stream?streams={'/'.join(streams)}"


class KlineStream:
    def __init__(
        self,
        base_ws: str,
        symbols: list[str],
        tfs: list[str],
        on_closed: Callable[[list[Candle]], Awaitable[None]],
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
        streams_per_conn: int = 150,
    ):
        self._urls = [combined_url(base_ws, c) for c in chunk(stream_names(symbols, tfs), streams_per_conn)]
        self._on_closed = on_closed
        self._on_reconnect = on_reconnect
        self._queue: asyncio.Queue[Candle] = asyncio.Queue()
        self._stop = asyncio.Event()
        self.last_message_at = time.time()
        self.closed_count = 0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._conn_loop(i, url)) for i, url in enumerate(self._urls)]
        tasks.append(asyncio.create_task(self._writer()))
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _conn_loop(self, idx: int, url: str) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # websockets < 13
            from websockets import connect  # type: ignore[no-redef]

        delay = 1.0
        first = True
        while not self._stop.is_set():
            try:
                async with connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                    log.info("ws[%d] connected (%d streams)", idx, url.count("@kline_"))
                    if not first and self._on_reconnect:
                        # candles that closed while disconnected come from REST
                        asyncio.create_task(self._on_reconnect())
                    first, delay = False, 1.0
                    opened = time.time()
                    async for raw in ws:
                        self.last_message_at = time.time()
                        self._handle(raw)
                        if time.time() - opened > MAX_CONN_AGE_S:
                            log.info("ws[%d] rotating before 24h limit", idx)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ws[%d] error: %s; reconnect in %.0fs", idx, exc, delay)
            if self._stop.is_set():
                break
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 60)

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
            if "data" not in msg and "k" not in msg:
                return
            candle, closed = parse_binance_ws_kline(msg)
        except (KeyError, ValueError, TypeError) as exc:
            log.debug("ws parse skip: %s", exc)
            return
        if closed:
            self._queue.put_nowait(candle)

    async def _writer(self) -> None:
        while not self._stop.is_set():
            batch = [await self._queue.get()]
            await asyncio.sleep(0.5)  # let candles of the same close moment gather
            while not self._queue.empty() and len(batch) < 500:
                batch.append(self._queue.get_nowait())
            try:
                await self._on_closed(batch)
                self.closed_count += len(batch)
            except Exception:
                log.exception("failed to persist %d streamed candles", len(batch))
