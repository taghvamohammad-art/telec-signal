"""LBank futures market data through ccxt (public, no keys in this phase)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class LBankFutures:
    def __init__(self) -> None:
        import ccxt.async_support as ccxt  # imported lazily so tests run without ccxt

        self._ex = ccxt.lbank({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        # load_markets() would also call fetchCurrencies (spot withdraw config endpoint),
        # which is unrelated to futures and sometimes down; we only need markets.
        self._ex.has["fetchCurrencies"] = False

    async def close(self) -> None:
        await self._ex.close()

    async def swap_markets(self) -> dict:
        markets = await self._ex.load_markets(reload=True)
        swaps = {k: m for k, m in markets.items() if m.get("swap")}
        log.info("lbank: %d swap markets", len(swaps))
        return swaps

    async def ticker(self, unified_symbol: str) -> dict:
        return await self._ex.fetch_ticker(unified_symbol)

    async def order_book(self, unified_symbol: str, limit: int = 50) -> dict:
        return await self._ex.fetch_order_book(unified_symbol, limit)

    async def contract_size(self, unified_symbol: str) -> float:
        """Base units per contract (order-book amounts may be in contracts)."""
        if not self._ex.markets:
            await self._ex.load_markets()
        m = self._ex.markets.get(unified_symbol) or {}
        return float(m.get("contractSize") or 1.0)
