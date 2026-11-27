"""Donchian-ensemble trend following, after Zarattini, Pagani & Barbon (2025),
"Catching Crypto Trends: A Tactical Approach for Bitcoin and Altcoins" (SSRN 5209907).

Rules as published (daily bars):
  * 9 lookbacks per coin: 5, 10, 20, 30, 60, 90, 150, 250, 360 days
  * a slice enters long when the daily close is above the highest high of the
    previous L days
  * its stop starts at the channel midpoint (HH+LL)/2 at entry and is raised
    daily to the current midpoint, never lowered
  * the slice exits on the first close below its stop
  * a coin's exposure is the share of its 9 slices that are on (0..1)
  * positions are scaled to a 25% annualized volatility target (90-day vol)

The paper is long-only. The short side here is the exact mirror and is
always reported separately, because it is our extension, not the paper's.
Decisions use the close of day t and are applied to the return of day t+1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
DAYS_PER_YEAR = 365
BASE_FUNDING_PER_DAY = 0.0003  # 0.01% per 8h, the usual baseline, when history is missing


def slice_positions(high: np.ndarray, low: np.ndarray, close: np.ndarray, lookback: int, side: int = 1) -> np.ndarray:
    """1 while the slice is in a position (decided at each close), else 0."""
    n = len(close)
    hh = pd.Series(high).rolling(lookback).max().shift(1).to_numpy()
    ll = pd.Series(low).rolling(lookback).min().shift(1).to_numpy()
    mid = (hh + ll) / 2
    pos = np.zeros(n)
    inside, stop = False, 0.0
    for t in range(n):
        if math.isnan(mid[t]):
            continue
        if not inside:
            if (side == 1 and close[t] > hh[t]) or (side == -1 and close[t] < ll[t]):
                inside, stop = True, mid[t]
        else:
            stop = max(stop, mid[t]) if side == 1 else min(stop, mid[t])
            if (side == 1 and close[t] < stop) or (side == -1 and close[t] > stop):
                inside = False
        pos[t] = 1.0 if inside else 0.0
    return pos


def coin_exposure(df: pd.DataFrame, side: int = 1, lookbacks=LOOKBACKS) -> tuple[pd.Series, int]:
    """(share of slices on, number of slice entries) for one coin's daily frame."""
    h, l, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    slices = [slice_positions(h, l, c, L, side) for L in lookbacks]
    entries = int(sum(((s[1:] == 1) & (s[:-1] == 0)).sum() for s in slices))
    return pd.Series(np.mean(slices, axis=0), index=df.index), entries


def annual_vol(close: pd.Series, window: int = 90) -> pd.Series:
    r = np.log(close).diff()
    return r.rolling(window, min_periods=30).std() * math.sqrt(DAYS_PER_YEAR)


@dataclass
class Params:
    top_n: int = 20
    target_vol: float = 0.25
    max_gross: float = 1.0
    cost: float = 0.001  # per unit of turnover: taker fee + slippage
    mode: str = "long"  # long | short | both
    min_history: int = 90
    vol_floor: float = 0.2
    lookbacks: tuple = LOOKBACKS


@dataclass
class Result:
    equity: pd.Series
    weights: pd.DataFrame
    stats: dict
    yearly: dict
    btc_yearly: dict
    entries_per_month: float
    coin_entries_per_month: float
    notes: list = field(default_factory=list)


def _stats(ret: pd.Series) -> dict:
    ret = ret.dropna()
    if ret.empty:
        return {}
    eq = (1 + ret).cumprod()
    years = len(ret) / DAYS_PER_YEAR
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = ret.std() * math.sqrt(DAYS_PER_YEAR)
    dd = (eq / eq.cummax() - 1).min()
    return {"cagr": round(100 * cagr, 2), "vol": round(100 * vol, 2),
            "sharpe": round(ret.mean() / ret.std() * math.sqrt(DAYS_PER_YEAR), 2) if ret.std() > 0 else None,
            "max_dd": round(100 * dd, 2), "total": round(100 * (eq.iloc[-1] - 1), 2),
            "days": int(len(ret)), "exposure_days_pct": None}


def _yearly(ret: pd.Series) -> dict:
    idx = pd.to_datetime(ret.index, unit="ms", utc=True)
    s = pd.Series(ret.to_numpy(), index=idx)
    return {str(y): round(100 * ((1 + g).prod() - 1), 2) for y, g in s.groupby(s.index.year)}


def run(daily: dict[str, pd.DataFrame], funding: dict[str, pd.Series] | None = None, p: Params = Params(),
        start_ms: int | None = None) -> Result:
    """daily: symbol -> frame (open, high, low, close, quote_volume) indexed by day open time (ms).
    funding: symbol -> series of funding paid per day (sum of the day's rates)."""
    funding = funding or {}
    dates = sorted(set().union(*[set(df.index) for df in daily.values()]))
    idx = pd.Index(dates)
    close = pd.DataFrame({s: df["close"] for s, df in daily.items()}).reindex(idx)
    qv = pd.DataFrame({s: df["quote_volume"] for s, df in daily.items()}).reindex(idx)
    rets = close.pct_change(fill_method=None)

    sides = {"long": (1,), "short": (-1,), "both": (1, -1)}[p.mode]
    exp = pd.DataFrame(0.0, index=idx, columns=close.columns)
    entries = 0
    for s, df in daily.items():
        for side in sides:
            e, n_ent = coin_exposure(df, side, p.lookbacks)
            exp[s] = exp[s].add(side * e.reindex(idx).fillna(0.0), fill_value=0.0)
            entries += n_ent

    vol = close.apply(annual_vol).clip(lower=p.vol_floor)
    history = close.notna().cumsum()
    liquid = qv.rolling(30, min_periods=10).mean().shift(1)  # ranking uses past volume only

    weights = pd.DataFrame(0.0, index=idx, columns=close.columns)
    for t in idx:
        elig = [s for s in close.columns if history.at[t, s] >= p.min_history and not math.isnan(liquid.at[t, s] or float("nan"))]
        if not elig:
            continue
        top = sorted(elig, key=lambda s: liquid.at[t, s], reverse=True)[: p.top_n]
        w = {s: exp.at[t, s] * p.target_vol / vol.at[t, s] / p.top_n for s in top if exp.at[t, s] != 0}
        gross = sum(abs(x) for x in w.values())
        scale = p.max_gross / gross if gross > p.max_gross else 1.0
        for s, x in w.items():
            weights.at[t, s] = x * scale

    held = weights.shift(1).fillna(0.0)  # decided at close t, earns return of t+1
    fund = pd.DataFrame({s: funding.get(s, pd.Series(dtype=float)) for s in close.columns}).reindex(idx)
    fund = fund.where(fund.notna(), BASE_FUNDING_PER_DAY)
    gross_ret = (held * rets.fillna(0.0)).sum(axis=1)
    fund_cost = (held * fund).sum(axis=1)  # longs pay positive funding, shorts receive it
    turnover = (weights - weights.shift(1).fillna(0.0)).abs().sum(axis=1).shift(1).fillna(0.0)
    ret = gross_ret - fund_cost - p.cost * turnover

    if start_ms is not None:
        ret = ret[ret.index >= start_ms]
    first_decision = weights.abs().sum(axis=1).gt(0)  # first close with a position decided
    if first_decision.any():
        ret = ret[ret.index >= first_decision[first_decision].index[0]]
    equity = (1 + ret).cumprod()

    btc = rets.get("BTCUSDT", pd.Series(dtype=float)).reindex(ret.index).fillna(0.0)
    months = max(len(ret) / 30.4, 1)
    coin_entries = int(((exp != 0) & (exp.shift(1).fillna(0) == 0)).loc[ret.index].sum().sum())
    stats = _stats(ret)
    stats["exposure_days_pct"] = round(100 * float(held.loc[ret.index].abs().sum(axis=1).gt(0).mean()), 1)
    stats["avg_gross"] = round(float(held.loc[ret.index].abs().sum(axis=1).mean()), 3)
    stats["cost_drag_pct_per_year"] = round(100 * float((p.cost * turnover).loc[ret.index].mean()) * DAYS_PER_YEAR, 2)
    stats["funding_drag_pct_per_year"] = round(100 * float(fund_cost.loc[ret.index].mean()) * DAYS_PER_YEAR, 2)
    stats["btc"] = _stats(btc)
    return Result(equity=equity, weights=weights.loc[ret.index], stats=stats, yearly=_yearly(ret),
                  btc_yearly=_yearly(btc), entries_per_month=round(entries / max(len(idx) / 30.4, 1), 1),
                  coin_entries_per_month=round(coin_entries / months, 1))
