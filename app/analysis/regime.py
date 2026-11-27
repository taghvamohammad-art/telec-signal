"""Market regime of one timeframe, from the last closed bar."""

from __future__ import annotations

from enum import Enum

import pandas as pd


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    SQUEEZE = "squeeze"
    CHAOTIC = "chaotic"


ADX_TREND = 22.0
SQUEEZE_PCTL = 0.20  # bb width in the lowest 20% of the lookback
CHAOS_ATR_MULT = 2.2  # atr% above 2.2x its median


def classify(df: pd.DataFrame, lookback: int = 120) -> Regime:
    """df must already carry add_indicators() columns."""
    last = df.iloc[-1]
    hist = df.iloc[-lookback:]
    atr_med = hist["atr_pct"].median()
    if pd.notna(atr_med) and last["atr_pct"] > CHAOS_ATR_MULT * atr_med:
        return Regime.CHAOTIC
    # Trend before squeeze: a smooth trend has tight bands but is not a squeeze.
    if last["adx"] >= ADX_TREND:
        if last["ema50"] > last["ema200"] and last["close"] > last["ema50"] and last["pdi"] > last["mdi"]:
            return Regime.TREND_UP
        if last["ema50"] < last["ema200"] and last["close"] < last["ema50"] and last["mdi"] > last["pdi"]:
            return Regime.TREND_DOWN
    widths = hist["bb_width"].dropna()
    if len(widths) >= 30 and last["bb_width"] <= widths.quantile(SQUEEZE_PCTL):
        return Regime.SQUEEZE
    return Regime.RANGE


def htf_bias(df: pd.DataFrame) -> int:
    """+1 long bias, -1 short bias, 0 neutral, from a higher timeframe (4h/1d)."""
    last = df.iloc[-1]
    if pd.isna(last["ema200"]):
        # not enough history for the 200 EMA: fall back to 50 EMA slope
        e = df["ema50"].dropna()
        if len(e) < 6:
            return 0
        return 1 if e.iloc[-1] > e.iloc[-6] and last["close"] > e.iloc[-1] else -1 if e.iloc[-1] < e.iloc[-6] and last["close"] < e.iloc[-1] else 0
    if last["ema50"] > last["ema200"] and last["close"] > last["ema200"]:
        return 1
    if last["ema50"] < last["ema200"] and last["close"] < last["ema200"]:
        return -1
    return 0
