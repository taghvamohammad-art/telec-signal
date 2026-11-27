"""Causal technical indicators.

Every value at row t depends only on rows <= t (tested in tests/test_analysis.py).
Swing points are reported on the bar where they become CONFIRMED, never on the
pivot bar itself, so nothing here can see the future.

Input frame columns: open, high, low, close, volume (index: open_time in ms, ascending).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average (used by RSI, ATR, ADX)."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = rma(d.clip(lower=0), n)
    down = rma(-d.clip(upper=0), n)
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(down != 0, 100.0).where(up.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = rma(true_range(df), n)
    pdi = 100 * rma(plus_dm, n) / tr
    mdi = 100 * rma(minus_dm, n) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": rma(dx, n), "pdi": pdi, "mdi": mdi})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    return pd.DataFrame({"bb_mid": mid, "bb_up": up, "bb_lo": lo, "bb_width": (up - lo) / mid})


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.DataFrame:
    m = ema(close, fast) - ema(close, slow)
    s = ema(m, sig)
    return pd.DataFrame({"macd": m, "macd_sig": s, "macd_hist": m - s})


def donchian_prev(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """High/low of the previous n bars (excluding the current one)."""
    return pd.DataFrame({"dc_hi": df["high"].rolling(n).max().shift(1), "dc_lo": df["low"].rolling(n).min().shift(1)})


def rel_volume(vol: pd.Series, n: int = 20) -> pd.Series:
    """Current volume / average of the previous n bars."""
    return vol / vol.rolling(n).mean().shift(1)


def vwap_daily(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored at each UTC day start."""
    day = (df.index.to_numpy() // 86_400_000)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum()
    return pd.Series((pv / v.replace(0, np.nan)).to_numpy(), index=df.index)


def confirmed_pivots(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Swing highs/lows confirmed k bars after the pivot.

    Columns:
      ph / pl          : pivot price, placed on the confirmation bar (NaN otherwise)
      ph_pos / pl_pos  : integer row position of the pivot bar itself
      last_ph / last_pl: most recent confirmed pivot price (forward-filled)
    """
    w = 2 * k + 1
    hi, lo = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    n = len(df)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    ph_pos = np.full(n, np.nan)
    pl_pos = np.full(n, np.nan)
    if n >= w:
        # row j of the window view covers bars [j, j+w-1]; its centre j+k is the
        # candidate pivot, confirmed at bar t = j+w-1 (right side complete)
        wh = np.lib.stride_tricks.sliding_window_view(hi, w)
        wl = np.lib.stride_tricks.sliding_window_view(lo, w)
        ch, cl = wh[:, k], wl[:, k]
        is_ph = (ch == wh.max(axis=1)) & ((wh == ch[:, None]).sum(axis=1) == 1)
        is_pl = (cl == wl.min(axis=1)) & ((wl == cl[:, None]).sum(axis=1) == 1)
        t = np.arange(w - 1, n)
        ph[t[is_ph]], ph_pos[t[is_ph]] = ch[is_ph], (t - k)[is_ph]
        pl[t[is_pl]], pl_pos[t[is_pl]] = cl[is_pl], (t - k)[is_pl]
    out = pd.DataFrame({"ph": ph, "pl": pl, "ph_pos": ph_pos, "pl_pos": pl_pos}, index=df.index)
    out["last_ph"] = out["ph"].ffill()
    out["last_pl"] = out["pl"].ffill()
    return out


def add_indicators(df: pd.DataFrame, pivot_k: int = 3) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["ema20"], out["ema50"], out["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    out["rsi"] = rsi(c)
    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / c * 100
    out = out.join(adx(out)).join(bollinger(c)).join(macd(c)).join(donchian_prev(out))
    out["rvol"] = rel_volume(out["volume"])
    out["vwap"] = vwap_daily(out)
    out = out.join(confirmed_pivots(out, pivot_k))
    return out
