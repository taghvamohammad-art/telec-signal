"""Settings loaded from environment variables (and .env if python-dotenv is installed)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional at import time so unit tests run without it
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://telec:telec@localhost:5432/telec_signal"))

    # Market coverage
    universe_size: int = field(default_factory=lambda: _env_int("UNIVERSE_SIZE", 100))
    analysis_size: int = field(default_factory=lambda: _env_int("ANALYSIS_SIZE", 40))
    timeframes: list[str] = field(default_factory=lambda: _env_list("TIMEFRAMES", "15m,1h,4h,1d"))
    require_lbank: bool = field(default_factory=lambda: _env_bool("REQUIRE_LBANK", True))
    selection_volatility_weight: float = field(default_factory=lambda: float(_env("SELECTION_VOLATILITY_WEIGHT", "0")))
    always_include: list[str] = field(default_factory=lambda: _env_list("ALWAYS_INCLUDE", "BTCUSDT,ETHUSDT"))

    # History
    backfill_days: int = field(default_factory=lambda: _env_int("BACKFILL_DAYS", 120))

    # Endpoints (Binance split its futures WebSocket by path in 2026; klines live under /market)
    binance_rest: str = field(default_factory=lambda: _env("BINANCE_REST", "https://fapi.binance.com"))
    binance_ws: str = field(default_factory=lambda: _env("BINANCE_WS", "wss://fstream.binance.com/market"))
    ws_streams_per_conn: int = field(default_factory=lambda: _env_int("WS_STREAMS_PER_CONN", 150))

    # Admin alerts (Telegram). Empty = log only.
    admin_bot_token: str = field(default_factory=lambda: _env("ADMIN_BOT_TOKEN", ""))
    admin_chat_id: str = field(default_factory=lambda: _env("ADMIN_CHAT_ID", ""))

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))


def load_settings() -> Settings:
    s = Settings()
    if s.analysis_size > s.universe_size:
        raise ValueError("ANALYSIS_SIZE cannot exceed UNIVERSE_SIZE")
    return s
