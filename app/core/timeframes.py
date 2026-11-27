"""Timeframe helpers. All timestamps are UTC epoch milliseconds."""

from __future__ import annotations

TF_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def tf_ms(tf: str) -> int:
    try:
        return TF_MS[tf]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {tf}") from exc


def align(ts_ms: int, tf: str) -> int:
    """Floor a timestamp to the start of its candle."""
    step = tf_ms(tf)
    return ts_ms - (ts_ms % step)


def is_aligned(ts_ms: int, tf: str) -> bool:
    return ts_ms % tf_ms(tf) == 0


def last_closed_open_time(now_ms: int, tf: str) -> int:
    """Open time of the most recent candle that has fully closed at now_ms."""
    return align(now_ms, tf) - tf_ms(tf)


def expected_open_times(start_ms: int, end_ms: int, tf: str) -> list[int]:
    """All candle open times in [start, end], both aligned and inclusive."""
    step = tf_ms(tf)
    first = align(start_ms, tf)
    if first < start_ms:
        first += step
    return list(range(first, align(end_ms, tf) + 1, step))
