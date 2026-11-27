"""Pure analysis of one symbol at one moment (no I/O), so live and backtest share it."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.analysis.indicators import add_indicators
from app.analysis.regime import Regime, classify, htf_bias
from app.analysis.scoring import STRONG, Candidate, build_candidate, market_factor

NO_TRADE_REGIMES = {Regime.CHAOTIC, Regime.RANGE}  # range: worst result in the first backtest
from app.analysis.strategies import Context, collect_votes
from app.core.timeframes import tf_ms

MIN_BARS = {"15m": 120, "1h": 120, "4h": 60, "1d": 30}


@dataclass
class Diagnosis:
    symbol: str
    regime_1h: str
    bias: int
    votes: list
    result: str  # "ok" or why no signal


def closed_only(df: pd.DataFrame, tf: str, now_ms: int) -> pd.DataFrame:
    """Drop any bar not fully closed at now_ms (guards against look-ahead in higher timeframes)."""
    return df[df.index + tf_ms(tf) <= now_ms]


def prepare(frames: dict[str, pd.DataFrame], now_ms: int) -> dict[str, pd.DataFrame] | None:
    out = {}
    for tf, need in MIN_BARS.items():
        df = frames.get(tf)
        if df is None:
            return None
        df = closed_only(df, tf, now_ms)
        if len(df) < need:
            return None
        out[tf] = add_indicators(df)
    return out


def btc_context(btc: dict[str, pd.DataFrame] | None) -> tuple[str | None, float | None]:
    """(BTC 1h regime, BTC 1-hour move in 15m ATRs)."""
    if not btc:
        return None, None
    reg = classify(btc["1h"]).value
    m = btc["15m"]
    atr = m["atr"].iloc[-1]
    move = (m["close"].iloc[-1] - m["close"].iloc[-5]) / atr if atr and atr > 0 else None
    return reg, move


def btc_context_from_raw(btc_frames: dict[str, pd.DataFrame] | None, now_ms: int) -> tuple[str | None, float | None]:
    """Compute once per cycle and pass to analyze(btc_ctx=...)."""
    prep = prepare(btc_frames, now_ms) if btc_frames else None
    return btc_context(prep)


def analyze(
    symbol: str,
    frames: dict[str, pd.DataFrame],
    now_ms: int,
    btc_frames: dict[str, pd.DataFrame] | None = None,
    funding: float | None = None,
    weights: dict[str, float] | None = None,
    f_news: float = 1.0,
    btc_ctx: tuple[str | None, float | None] | None = None,
) -> tuple[Candidate | None, Diagnosis]:
    """frames / btc_frames: raw candles per timeframe (indicators are added here).
    Pass btc_ctx (from btc_context_from_raw) to avoid recomputing BTC for every symbol."""
    prep = prepare(frames, now_ms)
    if prep is None:
        return None, Diagnosis(symbol, "-", 0, [], "insufficient_data")
    if btc_ctx is None:
        btc_prep = prep if symbol == "BTCUSDT" else (prepare(btc_frames, now_ms) if btc_frames else None)
        btc_ctx = btc_context(btc_prep)

    return evaluate(symbol, prep, btc_ctx, funding, weights, f_news)


def evaluate(
    symbol: str,
    prep: dict[str, pd.DataFrame],
    btc_ctx,
    funding: float | None = None,
    weights: dict[str, float] | None = None,
    f_news: float = 1.0,
    regime: Regime | None = None,
    bias: int | None = None,
) -> tuple[Candidate | None, Diagnosis]:
    """Decision on frames that already carry indicators and contain only closed bars.

    Shared by the live engine and the backtest so both use identical rules.
    btc_ctx: (regime, move) or a zero-argument callable returning it (computed only when needed).
    """
    regime = classify(prep["1h"]) if regime is None else regime
    bias = htf_bias(prep["4h"]) if bias is None else bias
    ctx = Context(symbol, prep["15m"], prep["1h"], prep["4h"], prep["1d"], regime, bias)
    votes = collect_votes(ctx)
    diag = Diagnosis(symbol, regime.value, bias, votes, "")
    if regime in NO_TRADE_REGIMES:
        diag.result = regime.value
        return None, diag
    if not votes:
        diag.result = "no_votes"
        return None, diag

    btc_reg, btc_move = btc_ctx() if callable(btc_ctx) else btc_ctx
    strong_sides = {v.side for v in votes if v.role == "anchor" and v.s >= STRONG}
    side = next(iter(strong_sides)) if len(strong_sides) == 1 else max(votes, key=lambda v: v.s).side
    f_mkt, notes = market_factor(side, btc_reg, btc_move, funding, symbol == "BTCUSDT")
    entry = float(prep["15m"]["close"].iloc[-1])
    atr_1h = float(prep["1h"]["atr"].iloc[-1])
    cand, why = build_candidate(symbol, votes, entry, atr_1h, f_mkt, f_news, weights, notes)
    diag.result = why
    return cand, diag
