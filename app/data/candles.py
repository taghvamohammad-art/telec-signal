"""Parsing, validation and gap detection for candles.

Rules enforced here are the data-quality half of the anti look-ahead policy:
a candle is only accepted once it has fully closed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from app.core.models import Candle
from app.core.timeframes import expected_open_times, is_aligned


def parse_binance_rest_kline(symbol: str, tf: str, row: Sequence) -> Candle:
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, ...]
    return Candle(
        symbol=symbol,
        tf=tf,
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        quote_volume=float(row[7]),
        trades=int(row[8]),
    )


def parse_binance_ws_kline(msg: dict) -> tuple[Candle, bool]:
    """Parse a kline event (raw or combined-stream envelope). Returns (candle, is_closed)."""
    data = msg.get("data", msg)
    k = data["k"]
    candle = Candle(
        symbol=k["s"],
        tf=k["i"],
        open_time=int(k["t"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        quote_volume=float(k["q"]),
        trades=int(k["n"]),
    )
    return candle, bool(k["x"])


def validate_candle(c: Candle, now_ms: int) -> list[str]:
    problems: list[str] = []
    if not is_aligned(c.open_time, c.tf):
        problems.append("misaligned_open_time")
    if c.close_time >= now_ms:
        problems.append("not_closed")
    values = (c.open, c.high, c.low, c.close, c.volume, c.quote_volume)
    if not all(math.isfinite(v) for v in values):
        problems.append("non_finite")
        return problems
    if min(c.open, c.high, c.low, c.close) <= 0:
        problems.append("non_positive_price")
    if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.high < c.low:
        problems.append("inconsistent_ohlc")
    if c.volume < 0 or c.quote_volume < 0 or c.trades < 0:
        problems.append("negative_volume")
    return problems


def clean_batch(candles: Iterable[Candle], now_ms: int) -> tuple[list[Candle], list[tuple[Candle, list[str]]]]:
    """Validate, de-duplicate (last one wins) and sort. Returns (accepted, rejected)."""
    by_key: dict[tuple[str, str, int], Candle] = {}
    rejected: list[tuple[Candle, list[str]]] = []
    for c in candles:
        problems = validate_candle(c, now_ms)
        if problems:
            rejected.append((c, problems))
        else:
            by_key[c.key] = c
    accepted = sorted(by_key.values(), key=lambda c: (c.symbol, c.tf, c.open_time))
    return accepted, rejected


def find_gaps(open_times: Iterable[int], tf: str, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Missing candle ranges in [start, end] as (first_missing_open, last_missing_open)."""
    have = set(open_times)
    gaps: list[tuple[int, int]] = []
    run_start: int | None = None
    prev: int | None = None
    for t in expected_open_times(start_ms, end_ms, tf):
        if t in have:
            if run_start is not None:
                gaps.append((run_start, prev))  # type: ignore[arg-type]
                run_start = None
        else:
            if run_start is None:
                run_start = t
            prev = t
    if run_start is not None:
        gaps.append((run_start, prev))  # type: ignore[arg-type]
    return gaps
