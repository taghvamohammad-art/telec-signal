from __future__ import annotations

from dataclasses import dataclass

from app.core.timeframes import tf_ms


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    tf: str
    open_time: int  # UTC epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int

    @property
    def close_time(self) -> int:
        return self.open_time + tf_ms(self.tf) - 1

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.symbol, self.tf, self.open_time)


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    symbol: str  # Binance symbol, e.g. 1000PEPEUSDT
    base: str  # normalized base, e.g. PEPE
    multiplier: int  # 1000 for 1000PEPE
    quote_volume_24h: float
    range_pct_24h: float  # (high - low) / low * 100
    on_lbank: bool
    lbank_symbol: str | None  # ccxt unified symbol, e.g. PEPE/USDT:USDT
