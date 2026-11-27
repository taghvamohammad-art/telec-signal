"""Crowding tests on perpetual funding (long and short, held for days).

Funding is what longs pay shorts every 8h. Very positive = longs are crowded and
pay a lot to stay in; very negative = shorts are crowded.

1) Cross-sectional (market neutral): every `rebalance` days, among the `top_n` most
   liquid coins, short the `q` share with the highest recent funding and long the
   `q` share with the lowest. Shorts also COLLECT the funding, longs pay it.
2) Time-series contrarian: when a coin's recent funding is far above its own
   history (z > z_in), short it for `hold` days; far below (z < -z_in), long it.

Everything is decided at a day's close with data up to that close and earns the
next day's return. Costs: fee+slippage per unit traded, plus actual funding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

DAYS = 365


@dataclass(frozen=True)
class XSParams:
    top_n: int = 50
    q: float = 0.2             # share of coins in each leg
    window: int = 7            # days of funding averaged
    rebalance: int = 7         # days between rebalances
    cost: float = 0.001
    min_history: int = 60


@dataclass(frozen=True)
class TSParams:
    top_n: int = 50
    window: int = 3
    z_window: int = 90
    z_in: float = 2.0
    hold: int = 7
    target_vol: float = 0.25   # per coin, then divided by top_n
    cost: float = 0.001
    min_history: int = 120


def panel(daily: dict[str, pd.DataFrame], funding: dict[str, pd.Series]):
    idx = pd.Index(sorted(set().union(*[set(d.index) for d in daily.values()])))
    close = pd.DataFrame({s: d["close"] for s, d in daily.items()}).reindex(idx)
    qv = pd.DataFrame({s: d["quote_volume"] for s, d in daily.items()}).reindex(idx)
    fund = pd.DataFrame({s: funding.get(s, pd.Series(dtype=float)) for s in daily}).reindex(idx)
    # a coin's funding is "known" only on days it traded; missing funding stays NaN (no signal)
    fund = fund.where(close.notna())
    rets = close.pct_change(fill_method=None)
    liquid = qv.rolling(30, min_periods=10).mean()  # includes today's volume: known at today's close
    history = close.notna().cumsum()
    return idx, close, rets, fund, liquid, history


def _pnl(weights: pd.DataFrame, rets: pd.DataFrame, fund: pd.DataFrame, cost: float) -> pd.DataFrame:
    held = weights.shift(1).fillna(0.0)                         # decided at close t, earns t+1
    price = (held * rets.fillna(0.0)).sum(axis=1)
    carry = -(held * fund.fillna(0.0)).sum(axis=1)              # longs pay positive funding, shorts receive it
    turnover = (weights - weights.shift(1).fillna(0.0)).abs().sum(axis=1).shift(1).fillna(0.0)
    fees = cost * turnover
    long_leg = (held.clip(lower=0) * rets.fillna(0.0)).sum(axis=1) - (held.clip(lower=0) * fund.fillna(0.0)).sum(axis=1)
    short_leg = (held.clip(upper=0) * rets.fillna(0.0)).sum(axis=1) - (held.clip(upper=0) * fund.fillna(0.0)).sum(axis=1)
    out = pd.DataFrame({"ret": price + carry - fees, "price": price, "carry": carry, "fees": fees,
                        "long_leg": long_leg, "short_leg": short_leg,
                        "gross": held.abs().sum(axis=1), "net": held.sum(axis=1)})
    active = weights.abs().sum(axis=1).gt(0)
    return out[out.index >= active[active].index[0]] if active.any() else out.iloc[0:0]


def cross_sectional(daily, funding, p: XSParams = XSParams()) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx, close, rets, fund, liquid, history = panel(daily, funding)
    sig = fund.rolling(p.window, min_periods=p.window).mean()
    vol = rets.rolling(60, min_periods=30).std() * math.sqrt(DAYS)
    weights = pd.DataFrame(0.0, index=idx, columns=close.columns)
    current = pd.Series(0.0, index=close.columns)
    for i, t in enumerate(idx):
        if i % p.rebalance == 0:
            row_ok = (history.loc[t] >= p.min_history) & sig.loc[t].notna() & liquid.loc[t].notna() & vol.loc[t].gt(0)
            elig = liquid.loc[t][row_ok].sort_values(ascending=False).index[: p.top_n]
            current = pd.Series(0.0, index=close.columns)
            k = int(len(elig) * p.q)
            if k >= 2:
                ranked = sig.loc[t, elig].sort_values()
                longs, shorts = ranked.index[:k], ranked.index[-k:]
                inv = 1 / vol.loc[t]
                current[longs] = 0.5 * inv[longs] / inv[longs].sum()
                current[shorts] = -0.5 * inv[shorts] / inv[shorts].sum()
        weights.loc[t] = current
    return _pnl(weights, rets, fund, p.cost), weights


def time_series(daily, funding, p: TSParams = TSParams()) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx, close, rets, fund, liquid, history = panel(daily, funding)
    f = fund.rolling(p.window, min_periods=p.window).mean()
    mu = f.rolling(p.z_window, min_periods=p.z_window // 2).mean()
    sd = f.rolling(p.z_window, min_periods=p.z_window // 2).std()
    z = (f - mu) / sd.replace(0, np.nan)
    vol = (rets.rolling(60, min_periods=30).std() * math.sqrt(DAYS)).clip(lower=0.2)
    rank = liquid.rank(axis=1, ascending=False)
    ok = (history >= p.min_history) & (rank <= p.top_n)
    trig = pd.DataFrame(0.0, index=idx, columns=close.columns)
    trig[(z > p.z_in) & ok] = -1.0
    trig[(z < -p.z_in) & ok] = 1.0
    # hold `hold` days after the latest trigger (a new opposite trigger flips it)
    pos = trig.replace(0.0, np.nan).ffill(limit=p.hold - 1).fillna(0.0)
    weights = pos * p.target_vol / vol / p.top_n
    weights = weights.where(close.notna(), 0.0).fillna(0.0)
    return _pnl(weights, rets, fund, p.cost), weights


def stats(res: pd.DataFrame, btc: pd.Series | None = None) -> dict:
    ret = res["ret"].dropna()
    if len(ret) < 30 or ret.std() == 0:
        return {"days": len(ret)}
    eq = (1 + ret).cumprod()
    years = len(ret) / DAYS
    out = {"cagr": round(100 * (eq.iloc[-1] ** (1 / years) - 1), 2) if eq.iloc[-1] > 0 else -100.0,
           "vol": round(100 * ret.std() * math.sqrt(DAYS), 1),
           "sharpe": round(ret.mean() / ret.std() * math.sqrt(DAYS), 2),
           "max_dd": round(100 * (eq / eq.cummax() - 1).min(), 1),
           "price_pct_yr": round(100 * res["price"].mean() * DAYS, 1),
           "carry_pct_yr": round(100 * res["carry"].mean() * DAYS, 1),
           "fees_pct_yr": round(100 * res["fees"].mean() * DAYS, 1),
           "long_leg_pct_yr": round(100 * res["long_leg"].mean() * DAYS, 1),
           "short_leg_pct_yr": round(100 * res["short_leg"].mean() * DAYS, 1),
           "in_market_pct": round(100 * float(res["gross"].gt(0).mean()), 1),
           "days": int(len(ret))}
    if btc is not None:
        b = btc.reindex(ret.index)
        out["btc_corr"] = round(float(ret.corr(b)), 2) if b.notna().sum() > 30 else None
    return {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in out.items()}


def yearly(ret: pd.Series) -> dict:
    s = pd.Series(ret.to_numpy(), index=pd.to_datetime(ret.index, unit="ms", utc=True))
    return {str(y): round(float(100 * ((1 + g).prod() - 1)), 1) for y, g in s.groupby(s.index.year)}


def trades_per_month(weights: pd.DataFrame) -> float:
    w = weights.fillna(0.0)
    opened = ((w != 0) & (np.sign(w) != np.sign(w.shift(1).fillna(0.0)))).sum().sum()
    return round(float(opened) / max(len(w) / 30.4, 1), 1)
