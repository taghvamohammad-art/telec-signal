"""Binance USDⓈ-M futures public REST client (market data only, no keys)."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.core.models import Candle
from app.core.timeframes import tf_ms
from app.data.candles import parse_binance_rest_kline

log = logging.getLogger(__name__)

KLINES_MAX = 1500
WEIGHT_SOFT_LIMIT = 1800  # Binance futures IP limit is 2400/min; stay well below


class RegionBlocked(RuntimeError):
    """Binance answered 451/403: this server's location is restricted."""


class BinanceFutures:
    def __init__(self, base_url: str, timeout: float = 15.0, concurrency: int = 4):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._sem = asyncio.Semaphore(concurrency)
        self._used_weight = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None, retries: int = 5):
        delay = 1.0
        for attempt in range(1, retries + 1):
            if self._used_weight >= WEIGHT_SOFT_LIMIT:
                wait = 61 - (time.time() % 60)
                log.warning("binance weight %s, pausing %.0fs", self._used_weight, wait)
                await asyncio.sleep(wait)
                self._used_weight = 0
            async with self._sem:
                try:
                    r = await self._client.get(path, params=params)
                except httpx.TransportError as exc:
                    log.warning("binance %s transport error (%s), try %d", path, exc, attempt)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
            self._used_weight = int(r.headers.get("x-mbx-used-weight-1m", self._used_weight))
            if r.status_code in (451, 403):
                raise RegionBlocked(f"Binance refused {path} with {r.status_code}: server location is restricted")
            if r.status_code in (418, 429):
                wait = float(r.headers.get("retry-after", 60))
                log.error("binance rate limited (%s), sleeping %.0fs", r.status_code, wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"binance {path} failed after {retries} attempts")

    async def exchange_info(self) -> dict:
        return await self._get("/fapi/v1/exchangeInfo")

    async def tickers_24h(self) -> list[dict]:
        return await self._get("/fapi/v1/ticker/24hr")

    async def premium_index(self) -> list[dict]:
        """Mark price + last funding rate for all symbols (one call)."""
        return await self._get("/fapi/v1/premiumIndex")

    async def open_interest(self, symbol: str) -> dict:
        return await self._get("/fapi/v1/openInterest", {"symbol": symbol})

    async def klines(self, symbol: str, tf: str, start_ms: int, end_ms: int, limit: int = KLINES_MAX) -> list[Candle]:
        rows = await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": tf, "startTime": start_ms, "endTime": end_ms, "limit": limit},
        )
        return [parse_binance_rest_kline(symbol, tf, row) for row in rows]

    async def klines_range(self, symbol: str, tf: str, start_ms: int, end_ms: int) -> list[Candle]:
        """All candles with open_time in [start, end], paginated."""
        step = tf_ms(tf)
        out: list[Candle] = []
        cursor = start_ms
        while cursor <= end_ms:
            page = await self.klines(symbol, tf, cursor, end_ms)
            if not page:
                break
            out.extend(page)
            nxt = page[-1].open_time + step
            if nxt <= cursor:
                break
            cursor = nxt
            if len(page) < KLINES_MAX:
                break
        return [c for c in out if start_ms <= c.open_time <= end_ms]
