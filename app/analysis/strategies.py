"""Analysis styles. Each looks at the last CLOSED bars and returns a vote or None.

A vote: side (+1 long / -1 short), strength s in [0, 1], a structural stop, and a
short reason. Only "core" styles count toward the independent-confirmation rule;
filter styles (VWAP/volume) add to the score but cannot make a signal on their own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from app.analysis.regime import Regime


@dataclass(frozen=True)
class Vote:
    style: str
    side: int
    s: float
    stop: float | None
    reason: str
    core: bool = True


@dataclass
class Context:
    symbol: str
    m15: pd.DataFrame
    h1: pd.DataFrame
    h4: pd.DataFrame
    d1: pd.DataFrame
    regime_1h: Regime
    bias: int  # from 4h, 0 = neutral


def _clip(x: float) -> float:
    return 0.0 if math.isnan(x) else max(0.0, min(1.0, x))


def _ok(*vals) -> bool:
    return all(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in vals)


# ---- 1. trend pullback ------------------------------------------------
def trend_pullback(ctx: Context) -> Vote | None:
    side = {Regime.TREND_UP: 1, Regime.TREND_DOWN: -1}.get(ctx.regime_1h)
    if side is None or ctx.bias == -side:
        return None
    h, m = ctx.h1, ctx.m15
    last_h, recent = h.iloc[-1], h.iloc[-4:]
    lm, pm = m.iloc[-1], m.iloc[-2]
    if not _ok(last_h["ema20"], last_h["ema50"], last_h["atr"], lm["ema20"], pm["ema20"], lm["rsi"]):
        return None
    if side == 1:
        touched20 = (recent["low"] <= recent["ema20"] * 1.002).any()
        touched50 = (recent["low"] <= recent["ema50"] * 1.002).any()
        intact = last_h["close"] > last_h["ema50"] * 0.995
        trigger = lm["close"] > lm["ema20"] and (pm["close"] <= pm["ema20"] or lm["close"] > pm["high"]) and lm["rsi"] > 50
        stop = min(recent["low"].min(), last_h["last_pl"] if last_h["last_pl"] < lm["close"] else math.inf) - 0.25 * last_h["atr"]
    else:
        touched20 = (recent["high"] >= recent["ema20"] * 0.998).any()
        touched50 = (recent["high"] >= recent["ema50"] * 0.998).any()
        intact = last_h["close"] < last_h["ema50"] * 1.005
        trigger = lm["close"] < lm["ema20"] and (pm["close"] >= pm["ema20"] or lm["close"] < pm["low"]) and lm["rsi"] < 50
        stop = max(recent["high"].max(), last_h["last_ph"] if last_h["last_ph"] > lm["close"] else -math.inf) + 0.25 * last_h["atr"]
    if not (touched20 and intact and trigger):
        return None
    s = 0.4 * _clip(last_h["adx"] / 40) + 0.3 * (1.0 if touched50 else 0.6) + 0.3 * (1.0 if lm["rvol"] >= 1.0 else 0.6)
    return Vote("trend_pullback", side, round(s, 3), float(stop), f"پولبک به EMA در روند {'صعودی' if side > 0 else 'نزولی'} ۱ساعته، تریگر ۱۵دقیقه")


# ---- 2. breakout ------------------------------------------------------
def breakout(ctx: Context) -> Vote | None:
    if ctx.regime_1h == Regime.CHAOTIC:
        return None
    h = ctx.h1
    last = h.iloc[-1]
    if not _ok(last["dc_hi"], last["dc_lo"], last["atr"], last["rvol"]):
        return None
    widths = h["bb_width"].iloc[-120:].dropna()
    if len(widths) < 30:
        return None
    compressed = h["bb_width"].iloc[-7:-1].min() <= widths.quantile(0.25)
    if not compressed or last["rvol"] < 1.5:
        return None
    if last["close"] > last["dc_hi"]:
        side, level = 1, last["dc_hi"]
    elif last["close"] < last["dc_lo"]:
        side, level = -1, last["dc_lo"]
    else:
        return None
    if ctx.bias == -side:
        return None
    push = abs(last["close"] - level) / last["atr"]
    s = 0.4 * _clip(last["rvol"] / 3) + 0.3 * _clip(push / 1.0) + 0.3
    stop = level - side * 0.6 * last["atr"]
    return Vote("breakout", side, round(s, 3), float(stop), f"شکست محدوده فشرده ۱ساعته با حجم {last['rvol']:.1f} برابر")


# ---- 3. mean reversion (range only) ----------------------------------
def mean_reversion(ctx: Context) -> Vote | None:
    if ctx.regime_1h != Regime.RANGE:
        return None
    h, m = ctx.h1, ctx.m15
    recent = h.iloc[-2:]
    last_h, lm, pm = h.iloc[-1], m.iloc[-1], m.iloc[-2]
    if not _ok(last_h["atr"], last_h["bb_lo"], last_h["bb_up"]):
        return None
    if (recent["low"] < recent["bb_lo"]).any() and recent["rsi"].min() < 32 and lm["close"] > pm["high"] and ctx.bias != -1:
        side, extreme = 1, recent["rsi"].min()
        stop = h["low"].iloc[-3:].min() - 0.3 * last_h["atr"]
        s = 0.5 * _clip((32 - extreme) / 12) + 0.5
    elif (recent["high"] > recent["bb_up"]).any() and recent["rsi"].max() > 68 and lm["close"] < pm["low"] and ctx.bias != 1:
        side, extreme = -1, recent["rsi"].max()
        stop = h["high"].iloc[-3:].max() + 0.3 * last_h["atr"]
        s = 0.5 * _clip((extreme - 68) / 12) + 0.5
    else:
        return None
    return Vote("mean_reversion", side, round(s, 3), float(stop), f"برگشت از لبه باند بولینگر در رنج (RSI {extreme:.0f})")


# ---- 4. liquidity sweep (SMC, 15m) ------------------------------------
def liquidity_sweep(ctx: Context) -> Vote | None:
    if ctx.regime_1h == Regime.CHAOTIC:
        return None
    m = ctx.m15
    lm, prev = m.iloc[-1], m.iloc[-2]
    level_lo, level_hi = prev["last_pl"], prev["last_ph"]  # pivots known before this bar
    rng = lm["high"] - lm["low"]
    if not _ok(lm["atr"], lm["rvol"]) or rng <= 0 or lm["rvol"] < 1.2:
        return None
    if _ok(level_lo) and lm["low"] < level_lo < lm["close"] and (lm["close"] - lm["low"]) / rng > 0.5:
        side, depth, wick = 1, (level_lo - lm["low"]) / lm["atr"], (lm["close"] - lm["low"]) / rng
        stop = lm["low"] - 0.2 * lm["atr"]
    elif _ok(level_hi) and lm["close"] < level_hi < lm["high"] and (lm["high"] - lm["close"]) / rng > 0.5:
        side, depth, wick = -1, (lm["high"] - level_hi) / lm["atr"], (lm["high"] - lm["close"]) / rng
        stop = lm["high"] + 0.2 * lm["atr"]
    else:
        return None
    s = 0.4 * _clip(lm["rvol"] / 2.5) + 0.3 * _clip(wick) + 0.3 * _clip(depth)
    return Vote("liquidity_sweep", side, round(s, 3), float(stop), "شکار نقدینگی زیر/بالای سقف یا کف قبلی و برگشت")


# ---- 5. RSI divergence (1h) -------------------------------------------
def divergence(ctx: Context, k: int = 3) -> Vote | None:
    if ctx.regime_1h == Regime.CHAOTIC:
        return None
    h = ctx.h1.iloc[-80:]
    n = len(ctx.h1)
    last = h.iloc[-1]
    if not _ok(last["atr"]):
        return None
    for col_pos, col_price, side in (("pl_pos", "low", 1), ("ph_pos", "high", -1)):
        conf = h[col_pos].dropna()
        if len(conf) < 2:
            continue
        p1, p2 = int(conf.iloc[-2]), int(conf.iloc[-1])
        # the newest pivot must have just been confirmed
        if n - 1 - p2 > k + 2:
            continue
        price1, price2 = ctx.h1[col_price].iloc[p1], ctx.h1[col_price].iloc[p2]
        rsi1, rsi2 = ctx.h1["rsi"].iloc[p1], ctx.h1["rsi"].iloc[p2]
        if not _ok(rsi1, rsi2):
            continue
        if side == 1 and price2 < price1 and rsi2 > rsi1 + 2 and rsi2 < 45:
            s = 0.5 + 0.5 * _clip((rsi2 - rsi1) / 15)
            return Vote("divergence", 1, round(s, 3), float(price2 - 0.3 * last["atr"]), "واگرایی مثبت RSI در ۱ساعته")
        if side == -1 and price2 > price1 and rsi2 < rsi1 - 2 and rsi2 > 55:
            s = 0.5 + 0.5 * _clip((rsi1 - rsi2) / 15)
            return Vote("divergence", -1, round(s, 3), float(price2 + 0.3 * last["atr"]), "واگرایی منفی RSI در ۱ساعته")
    return None


# ---- filter: VWAP + volume (not core) ---------------------------------
def vwap_volume(ctx: Context) -> Vote | None:
    lm = ctx.m15.iloc[-1]
    if not _ok(lm["vwap"], lm["atr"], lm["rvol"]):
        return None
    dist = (lm["close"] - lm["vwap"]) / lm["atr"]
    if abs(dist) < 0.1:
        return None
    side = 1 if dist > 0 else -1
    s = 0.5 + 0.25 * _clip(abs(dist) / 2) + 0.25 * _clip((lm["rvol"] - 0.8) / 1.2)
    return Vote("vwap_volume", side, round(s, 3), None, "قیمت " + ("بالای" if side > 0 else "زیر") + " VWAP روزانه", core=False)


ALL_STYLES = (trend_pullback, breakout, mean_reversion, liquidity_sweep, divergence, vwap_volume)


def collect_votes(ctx: Context) -> list[Vote]:
    votes = []
    for fn in ALL_STYLES:
        v = fn(ctx)
        if v is not None and v.s > 0:
            votes.append(v)
    return votes
