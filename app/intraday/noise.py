"""Intraday momentum tests on 15m candles, long AND short.

A) Noise-area breakout (Zarattini, Aziz & Barbon 2024, adapted to a 24h market, UTC day):
   sigma_k = average |close_k / day_open - 1| at the same time of day over the previous N days.
   Boundaries: day_open * (1 ± mult * sigma_k). At each checkpoint close:
   above upper -> long, below lower -> short. Stop: long exits below max(upper, VWAP),
   short exits above min(lower, VWAP). Everything is closed at the end of the UTC day.
   Orders fill at the next bar's open. Size: target daily vol / recent daily vol, capped.

B) Half-hour momentum: the sign of the first 30 minutes (or of the day so far)
   is held during the last 30 minutes of the day.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

DAY = 86_400_000
BAR = 900_000
BARS = DAY // BAR  # 96


@dataclass(frozen=True)
class Params:
    lookback: int = 14          # days used for the noise area
    mult: float = 1.0           # width of the noise area
    step: int = 2               # checkpoint every `step` bars (2 = 30 min)
    target_vol: float = 0.02    # target daily volatility of one coin's position
    max_lev: float = 3.0
    cost: float = 0.0007        # fee + slippage per side, per unit of notional
    vol_days: int = 14


def to_days(df: pd.DataFrame) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """15m frame (index = open time ms) -> complete UTC days as (n_days, 96) matrices."""
    t = df.index.to_numpy(dtype=np.int64)
    day = t // DAY
    k = (t % DAY) // BAR
    counts = pd.Series(1, index=day).groupby(level=0).sum()
    full = counts.index[counts.to_numpy() == BARS].to_numpy()
    pos = np.searchsorted(full, day)
    ok = (pos < len(full)) & (full[np.minimum(pos, len(full) - 1)] == day)
    mats = {}
    for col in ("open", "high", "low", "close", "volume"):
        m = np.full((len(full), BARS), np.nan)
        m[pos[ok], k[ok]] = df[col].to_numpy(dtype=float)[ok]
        mats[col] = m
    return full * DAY, mats


def _prev_mean(x: np.ndarray, n: int) -> np.ndarray:
    """Row d = mean of rows d-n .. d-1 (NaN until n rows exist). Works on 1-D or 2-D."""
    cs = np.cumsum(np.nan_to_num(x), axis=0)
    cs = np.concatenate([np.zeros_like(cs[:1]), cs], axis=0)
    out = np.full(x.shape, np.nan)
    out[n:] = (cs[n:-1] - cs[:-n - 1]) / n
    return out


def prepare(mats: dict[str, np.ndarray], p: Params) -> dict[str, np.ndarray]:
    o, h, l_, c, v = (mats[k] for k in ("open", "high", "low", "close", "volume"))
    day_open = o[:, :1]
    move = np.abs(c / day_open - 1)
    sigma = _prev_mean(move, p.lookback)
    tp = (h + l_ + c) / 3
    vwap = np.cumsum(tp * v, axis=1) / np.maximum(np.cumsum(v, axis=1), 1e-12)
    day_close = c[:, -1]
    dret = np.concatenate([[np.nan], day_close[1:] / day_close[:-1] - 1])
    dvol = pd.Series(dret).rolling(p.vol_days).std().shift(1).to_numpy()  # past days only
    lev = np.where(dvol > 0, np.minimum(p.max_lev, p.target_vol / dvol), 0.0)
    return {"upper": day_open * (1 + p.mult * sigma), "lower": day_open * (1 - p.mult * sigma),
            "vwap": vwap, "lev": np.nan_to_num(lev)}


def noise_day(o: np.ndarray, c: np.ndarray, up: np.ndarray, lo: np.ndarray, vw: np.ndarray,
              step: int, cost: float) -> tuple[float, int]:
    """One day, unit leverage. Returns (return, number of position changes)."""
    pos, entry, ret, changes = 0, 0.0, 0.0, 0
    for k in range(step - 1, BARS - 1, step):
        px = c[k]
        if math.isnan(px) or math.isnan(up[k]):
            continue
        new = pos
        if pos == 1 and px < max(up[k], vw[k]):
            new = 0
        elif pos == -1 and px > min(lo[k], vw[k]):
            new = 0
        if new == 0:
            if px > up[k]:
                new = 1
            elif px < lo[k]:
                new = -1
        if new != pos:
            fill = o[k + 1]  # next bar's open
            if pos != 0:
                ret += pos * (fill / entry - 1)
            ret -= cost * abs(new - pos)
            changes += abs(new - pos)
            pos, entry = new, fill
    if pos != 0:
        ret += pos * (c[-1] / entry - 1) - cost
        changes += 1
    return ret, changes


def run_noise(df: pd.DataFrame, p: Params = Params()) -> pd.DataFrame:
    """Daily results for one coin: columns ret (leveraged, after costs), trades, lev."""
    days, m = to_days(df)
    x = prepare(m, p)
    rows = []
    for d in range(len(days)):
        lev = x["lev"][d]
        if lev <= 0 or np.isnan(x["upper"][d]).all():
            continue
        r, n = noise_day(m["open"][d], m["close"][d], x["upper"][d], x["lower"][d], x["vwap"][d], p.step, p.cost)
        rows.append((days[d], lev * r, n / 2, lev))
    return pd.DataFrame(rows, columns=["day", "ret", "trades", "lev"]).set_index("day")


def run_halfhour(df: pd.DataFrame, p: Params = Params(), signal: str = "first") -> pd.DataFrame:
    """Hold the sign of the first 30 min ('first') or of the day so far ('sofar') over the last 30 min."""
    days, m = to_days(df)
    x = prepare(m, p)
    o, c = m["open"], m["close"]
    if signal == "first":
        s = np.sign(c[:, 1] / o[:, 0] - 1)
    else:
        s = np.sign(c[:, BARS - 3] / o[:, 0] - 1)  # decided at 23:30 close of bar 93, filled at bar 94 open
    r = s * (c[:, -1] / o[:, BARS - 2] - 1) - 2 * p.cost * (s != 0)
    df_ = pd.DataFrame({"day": days, "ret": x["lev"] * r, "trades": (s != 0).astype(float), "lev": x["lev"]})
    return df_[df_["lev"] > 0].set_index("day")


def stats(ret: pd.Series, trades: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    if len(ret) < 20:
        return {"days": len(ret)}
    eq = (1 + ret).cumprod()
    years = len(ret) / 365
    out = {"days": len(ret),
           "cagr": round(100 * (eq.iloc[-1] ** (1 / years) - 1), 2) if eq.iloc[-1] > 0 else -100.0,
           "vol": round(100 * ret.std() * math.sqrt(365), 1),
           "sharpe": round(ret.mean() / ret.std() * math.sqrt(365), 2) if ret.std() > 0 else None,
           "max_dd": round(100 * (eq / eq.cummax() - 1).min(), 1),
           "win_days": round(100 * (ret > 0).sum() / max((ret != 0).sum(), 1), 1)}
    out = {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in out.items()}
    if trades is not None:
        out["trades_per_month"] = round(float(trades.reindex(ret.index).sum()) / (len(ret) / 30.4), 1)
    return out


def yearly(ret: pd.Series) -> dict:
    s = pd.Series(ret.to_numpy(), index=pd.to_datetime(ret.index, unit="ms", utc=True))
    return {str(y): round(float(100 * ((1 + g).prod() - 1)), 1) for y, g in s.groupby(s.index.year)}


def portfolio(per_coin: dict[str, pd.DataFrame]) -> tuple[pd.Series, pd.Series]:
    """Equal risk split across coins: the day's return is the mean over coins that traded that day."""
    rets = pd.DataFrame({s: d["ret"] for s, d in per_coin.items()}).sort_index()
    trades = pd.DataFrame({s: d["trades"] for s, d in per_coin.items()}).sort_index().fillna(0).sum(axis=1)
    return rets.mean(axis=1, skipna=True).fillna(0.0), trades
