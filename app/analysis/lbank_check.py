"""Check a candidate against the LBank market it will actually be traded on,
and translate its levels into LBank prices.

Analysis runs on Binance data. Before a signal is issued:
  1. the LBank contract must exist
  2. price difference, spread and order-book depth must be within limits
  3. entry/stop/targets are shifted by the price difference (keeping the R
     distances) and scaled when the two exchanges quote different multiples
     (Binance 1000PEPE vs LBank PEPE)
  4. the LBank price must not already be past the stop or TP1
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.data.universe import normalize_base


@dataclass(frozen=True)
class Limits:
    max_diff_pct: float = float(os.environ.get("LBANK_MAX_DIFF_PCT", "0.5"))
    max_spread_pct: float = float(os.environ.get("LBANK_MAX_SPREAD_PCT", "0.2"))
    min_depth_usdt: float = float(os.environ.get("LBANK_MIN_DEPTH_USDT", "5000"))
    depth_band_pct: float = 0.5


@dataclass
class LBankResult:
    ok: bool
    reason: str
    symbol: str | None = None
    price: float | None = None
    diff_pct: float | None = None
    spread_pct: float | None = None
    depth_usdt: float | None = None
    entry: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None


def multiplier_of(unified_symbol: str) -> int:
    return normalize_base(unified_symbol.split("/")[0])[1]


def book_metrics(book: dict, band_pct: float) -> tuple[float, float, float] | None:
    """(mid, spread %, min of bid/ask notional within band of mid) from a ccxt order book."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bid, ask = float(bids[0][0]), float(asks[0][0])
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    bid_depth = sum(float(p) * float(q) for p, q, *_ in bids if float(p) >= lo)
    ask_depth = sum(float(p) * float(q) for p, q, *_ in asks if float(p) <= hi)
    return mid, (ask - bid) / mid * 100, min(bid_depth, ask_depth)


def assess(
    lbank_symbol: str | None,
    book: dict | None,
    binance_price: float,
    binance_multiplier: int,
    side: int,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    limits: Limits = Limits(),
    contract_size: float = 1.0,
) -> LBankResult:
    if not lbank_symbol:
        return LBankResult(False, "not_on_lbank")
    if not book:
        return LBankResult(False, "no_lbank_book", lbank_symbol)
    m = book_metrics(book, limits.depth_band_pct)
    if m is None:
        return LBankResult(False, "empty_lbank_book", lbank_symbol)
    mid, spread, depth = m
    depth *= contract_size
    scale = multiplier_of(lbank_symbol) / binance_multiplier  # LBank price = Binance price x scale
    equiv = mid / scale  # LBank price in Binance units
    diff = (equiv - binance_price) / binance_price * 100
    res = LBankResult(False, "", lbank_symbol, mid, round(diff, 4), round(spread, 4), round(depth, 2))
    if abs(diff) > limits.max_diff_pct:
        res.reason = "lbank_price_diff"
        return res
    if spread > limits.max_spread_pct:
        res.reason = "lbank_spread"
        return res
    if depth < limits.min_depth_usdt:
        res.reason = "lbank_thin_book"
        return res
    shift = equiv - binance_price
    res.entry, res.stop, res.tp1, res.tp2 = ((lvl + shift) * scale for lvl in (entry, stop, tp1, tp2))
    if side * (mid - res.stop) <= 0 or side * (res.tp1 - mid) <= 0:
        res.reason = "lbank_price_past_levels"
        return res
    res.ok, res.reason = True, "ok"
    return res


async def check_on_lbank(lbank, lbank_symbol: str | None, binance_price: float, binance_multiplier: int,
                         side: int, entry: float, stop: float, tp1: float, tp2: float, limits: Limits = Limits()) -> LBankResult:
    """lbank: app.data.lbank.LBankFutures. Network problems give ok=False with reason lbank_unavailable."""
    if not lbank_symbol:
        return LBankResult(False, "not_on_lbank")
    try:
        book = await lbank.order_book(lbank_symbol, 50)
        size = await lbank.contract_size(lbank_symbol)
    except Exception:
        return LBankResult(False, "lbank_unavailable", lbank_symbol)
    return assess(lbank_symbol, book, binance_price, binance_multiplier, side, entry, stop, tp1, tp2, limits, size)
