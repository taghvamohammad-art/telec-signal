"""Strategy lab: many daily styles, tested the same way, with protection against luck.

Every style turns the market panel into target weights decided at a day's close
(earning the next day). The engine adds fees and actual funding. The universe is
every Binance perpetual that ever traded (dead coins included), ranked by past
volume each day, so there is no survivorship bias.

Discipline (decided BEFORE looking at any result):
  * discovery period: up to DISCOVERY_END. Holdout after it is shown only for styles
    that pass in discovery, so the holdout cannot be used to pick a winner.
  * pass = Sharpe >= 0.8 after costs, at least 60% of calendar years positive, and a
    Deflated Sharpe Ratio >= 0.90 (corrects for how many styles were tried).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

DAYS = 365
DISCOVERY_END = "2024-12-31"


@dataclass
class Ctx:
    close: pd.DataFrame
    rets: pd.DataFrame
    fund: pd.DataFrame
    qv: pd.DataFrame
    liquid: pd.DataFrame      # 30-day mean quote volume, known at the close
    rank: pd.DataFrame        # 1 = most liquid that day
    age: pd.DataFrame         # days since the coin's first candle
    vol: pd.DataFrame         # 60-day annualized vol, floored
    btc: pd.Series


def build_ctx(daily: dict[str, pd.DataFrame], funding: dict[str, pd.Series]) -> Ctx:
    idx = pd.Index(sorted(set().union(*[set(d.index) for d in daily.values()])))
    close = pd.DataFrame({s: d["close"] for s, d in daily.items()}).reindex(idx)
    qv = pd.DataFrame({s: d["quote_volume"] for s, d in daily.items()}).reindex(idx)
    fund = pd.DataFrame({s: funding.get(s, pd.Series(dtype=float)) for s in daily}).reindex(idx).where(close.notna())
    rets = close.pct_change(fill_method=None).clip(-0.95, 5.0)
    liquid = qv.rolling(30, min_periods=10).mean()
    rank = liquid.rank(axis=1, ascending=False)
    age = close.notna().cumsum().where(close.notna())
    vol = (rets.rolling(60, min_periods=30).std() * math.sqrt(DAYS)).clip(lower=0.2)
    btc = rets["BTCUSDT"] if "BTCUSDT" in rets else pd.Series(0.0, index=idx)
    return Ctx(close, rets, fund, qv, liquid, rank, age, vol, btc)


# ---- building blocks --------------------------------------------------------
def eligible(c: Ctx, top_n: int = 100, min_age: int = 60) -> pd.DataFrame:
    return (c.rank <= top_n) & (c.age >= min_age) & c.close.notna()


def every(weights: pd.DataFrame, days: int) -> pd.DataFrame:
    """Keep the weights of every `days`-th row and hold them in between."""
    if days <= 1:
        return weights
    keep = np.arange(len(weights)) % days == 0
    return weights.where(pd.Series(keep, index=weights.index), np.nan).ffill().fillna(0.0)


def long_short(c: Ctx, score: pd.DataFrame, elig: pd.DataFrame, q: float = 0.2, rebalance: int = 7) -> pd.DataFrame:
    """Market neutral: long the top q by score, short the bottom q; inverse-vol inside each leg; gross 1."""
    s = score.where(elig)
    n = s.notna().sum(axis=1)
    pct = s.rank(axis=1, pct=True)
    ok = n >= 10
    inv = 1.0 / c.vol
    longs = inv.where(pct > 1 - q).where(ok, axis=0)
    shorts = inv.where(pct <= q).where(ok, axis=0)
    w = longs.div(longs.sum(axis=1), axis=0).fillna(0.0) * 0.5 - shorts.div(shorts.sum(axis=1), axis=0).fillna(0.0) * 0.5
    return every(w, rebalance)


def per_coin(c: Ctx, side: pd.DataFrame, elig: pd.DataFrame, n_slots: int, target_vol: float = 0.25) -> pd.DataFrame:
    """Directional, vol-targeted: each active coin gets target_vol / vol / n_slots."""
    w = side.where(elig, 0.0).fillna(0.0) * target_vol / c.vol / n_slots
    return w.fillna(0.0)


def btc_only(c: Ctx, side: pd.Series, target_vol: float = 0.5) -> pd.DataFrame:
    w = pd.DataFrame(0.0, index=c.close.index, columns=c.close.columns)
    w["BTCUSDT"] = (side * target_vol / c.vol["BTCUSDT"]).fillna(0.0).clip(-2, 2)
    return w


# ---- the styles (parameters fixed up front, no tuning) ---------------------------
def _past_ret(c: Ctx, days: int) -> pd.DataFrame:
    return c.close / c.close.shift(days) - 1


def s_xs_mom(days):
    return lambda c: long_short(c, _past_ret(c, days), eligible(c), rebalance=7)


def s_xs_rev(days, reb):
    return lambda c: long_short(c, -_past_ret(c, days), eligible(c), rebalance=reb)


def s_lowvol(c):
    return long_short(c, -c.vol, eligible(c), rebalance=7)


def s_size(c):  # small (less traded) minus big, inside the top 100
    return long_short(c, -np.log(c.liquid), eligible(c), rebalance=7)


def s_attention(c):  # abnormal volume: 7-day vs 90-day
    ab = c.qv.rolling(7, min_periods=5).mean() / c.qv.rolling(90, min_periods=45).mean()
    return long_short(c, np.log(ab), eligible(c), rebalance=7)


def s_bab(c):  # betting against beta (to BTC)
    cov = c.rets.rolling(90, min_periods=45).cov(c.btc)
    beta = cov.div(c.btc.rolling(90, min_periods=45).var(), axis=0)
    return long_short(c, -beta, eligible(c), rebalance=7)


def s_max(c):  # lottery effect: avoid coins with the biggest recent single-day jump
    return long_short(c, -c.rets.rolling(30, min_periods=20).max(), eligible(c), rebalance=7)


def s_new_listing(c):
    """Short coins listed 14-90 days ago, hedged with an equal long in BTC."""
    young = (c.age >= 14) & (c.age <= 90) & (c.rank <= 150) & c.close.notna()
    n = young.sum(axis=1)
    w = (-young.astype(float)).div(n.where(n >= 3), axis=0).fillna(0.0) * 0.5
    w["BTCUSDT"] = w["BTCUSDT"] + (n >= 3).astype(float) * 0.5
    return every(w, 1)


def s_tsmom(days, top_n=30):
    def f(c):
        side = np.sign(_past_ret(c, days))
        return per_coin(c, side, eligible(c, top_n), top_n)
    return f


def s_tsmom_long_bear_short(c):
    """Trend per coin, but shorts only when BTC itself is below its 200-day average."""
    side = np.sign(_past_ret(c, 30))
    bull = c.close["BTCUSDT"] > c.close["BTCUSDT"].rolling(200, min_periods=150).mean()
    side = side.where(side > 0, side.mul((~bull).astype(float), axis=0))  # shorts zeroed while BTC is in an uptrend
    return per_coin(c, side, eligible(c, 30), 30)


def s_btc_ma(c):
    b = c.close["BTCUSDT"]
    return btc_only(c, np.sign(b - b.rolling(50, min_periods=40).mean()))


def s_btc_weekend(c):
    """Long BTC only over the weekend (decided Friday close, out Sunday close)."""
    dow = pd.to_datetime(c.close.index, unit="ms", utc=True).dayofweek
    return btc_only(c, pd.Series(np.where((dow == 4) | (dow == 5), 1.0, 0.0), index=c.close.index))


def s_alt_rotation(c):
    """Alts vs BTC: hold top-20 alts (long) vs BTC (short) when alts led BTC over 30 days, else the reverse."""
    elig = eligible(c, 20)
    alt = c.rets.where(elig).drop(columns=["BTCUSDT"], errors="ignore").mean(axis=1)
    lead = (1 + alt).rolling(30).apply(np.prod, raw=True) - (1 + c.btc).rolling(30).apply(np.prod, raw=True)
    side = np.sign(lead)
    n = elig.drop(columns=["BTCUSDT"], errors="ignore").sum(axis=1)
    w = elig.astype(float).drop(columns=["BTCUSDT"], errors="ignore").div(n.where(n > 0), axis=0).mul(0.5 * side, axis=0)
    w = w.reindex(columns=c.close.columns, fill_value=0.0).fillna(0.0)
    w["BTCUSDT"] = -0.5 * side.fillna(0.0) * (n > 0)
    return every(w, 7)


def s_carry_neutral(c):
    """Funding harvest, hedged: for the 10 liquid coins with the highest positive 7-day funding,
    long spot + short perp (price cancels, funding is earned). Modelled as a perp short whose
    price move is cancelled; see engine's `hedged` flag."""
    f7 = c.fund.rolling(7, min_periods=7).mean()
    elig = eligible(c, 60) & (f7 > 0)
    r = f7.where(elig).rank(axis=1, ascending=False)
    pick = (r <= 10)
    n = pick.sum(axis=1)
    w = -pick.astype(float).div(n.where(n > 0), axis=0).fillna(0.0)
    return every(w, 7)


STYLES: dict[str, tuple[str, Callable, bool]] = {
    # key: (Persian label, function, hedged-price flag)
    "xs_mom_7": ("مومنتوم مقایسه‌ای ۷ روزه", s_xs_mom(7), False),
    "xs_mom_28": ("مومنتوم مقایسه‌ای ۲۸ روزه", s_xs_mom(28), False),
    "xs_rev_1": ("برگشت کوتاه‌مدت ۱ روزه", s_xs_rev(1, 1), False),
    "xs_rev_7": ("برگشت کوتاه‌مدت ۷ روزه", s_xs_rev(7, 7), False),
    "lowvol": ("کم‌نوسان منهای پرنوسان", s_lowvol, False),
    "size": ("کوچک منهای بزرگ", s_size, False),
    "attention": ("حجم غیرعادی", s_attention, False),
    "bab": ("بتای پایین منهای بالا", s_bab, False),
    "max": ("اثر لاتاری (MAX)", s_max, False),
    "new_listing": ("شورت ارزهای تازه‌لیست", s_new_listing, False),
    "tsmom_14": ("روند هر ارز ۱۴ روزه (دوطرفه)", s_tsmom(14), False),
    "tsmom_60": ("روند هر ارز ۶۰ روزه (دوطرفه)", s_tsmom(60), False),
    "tsmom_regime": ("روند ۳۰ روزه، شورت فقط در بازار نزولی", s_tsmom_long_bear_short, False),
    "btc_ma50": ("بیت‌کوین بالا/پایین میانگین ۵۰", s_btc_ma, False),
    "btc_weekend": ("بیت‌کوین فقط آخر هفته", s_btc_weekend, False),
    "alt_rotation": ("چرخش آلت‌کوین/بیت‌کوین", s_alt_rotation, False),
    "carry_neutral": ("آربیتراژ فاندینگ (اسپات+فیوچرز)", s_carry_neutral, True),
}


# ---- engine and statistics ---------------------------------------------------------
def pnl(c: Ctx, weights: pd.DataFrame, cost: float, hedged: bool = False) -> pd.DataFrame:
    held = weights.shift(1).fillna(0.0)
    price = (held * c.rets.fillna(0.0)).sum(axis=1)
    carry = -(held * c.fund.fillna(0.0)).sum(axis=1)
    turnover = (weights - weights.shift(1).fillna(0.0)).abs().sum(axis=1).shift(1).fillna(0.0)
    if hedged:   # spot leg cancels the price move; both legs pay fees (spot fee assumed 0.1%)
        price = price * 0.0
        fees = (cost + 0.001) * turnover
    else:
        fees = cost * turnover
    out = pd.DataFrame({"ret": price + carry - fees, "price": price, "carry": carry, "fees": fees,
                        "gross": held.abs().sum(axis=1)})
    active = weights.abs().sum(axis=1).gt(0)
    return out[out.index >= active[active].index[0]] if active.any() else out.iloc[0:0]


def sharpe(r: pd.Series) -> float | None:
    r = r.dropna()
    return float(r.mean() / r.std() * math.sqrt(DAYS)) if len(r) > 30 and r.std() > 0 else None


def deflated_sharpe(r: pd.Series, n_trials: int, sr_var: float) -> float | None:
    """Bailey & Lopez de Prado (2014): probability the true Sharpe is above what the best
    of n_trials random strategies would show by luck. Uses daily (non-annualized) Sharpe."""
    r = r.dropna()
    n = len(r)
    if n < 60 or r.std() == 0:
        return None
    from statistics import NormalDist

    nd = NormalDist()
    sr = r.mean() / r.std()
    g = 0.5772156649
    v = max(sr_var, 1e-12)
    sr0 = math.sqrt(v) * ((1 - g) * nd.inv_cdf(1 - 1 / max(n_trials, 2)) + g * nd.inv_cdf(1 - 1 / (max(n_trials, 2) * math.e)))
    skew, kurt = float(r.skew()), float(r.kurt()) + 3
    den = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-12))
    return nd.cdf((sr - sr0) * math.sqrt(n - 1) / den)


def summarize(res: pd.DataFrame, btc: pd.Series) -> dict:
    r = res["ret"].dropna()
    if len(r) < 30:
        return {"days": len(r)}
    eq = (1 + r).cumprod()
    years = len(r) / DAYS
    idx = pd.to_datetime(r.index, unit="ms", utc=True)
    yearly = {str(y): round(float(100 * ((1 + g).prod() - 1)), 1) for y, g in pd.Series(r.to_numpy(), index=idx).groupby(idx.year)}
    full_years = [v for y, v in yearly.items() if (idx.year == int(y)).sum() > 180]
    return {"sharpe": round(sharpe(r), 2) if sharpe(r) is not None else None,
            "cagr": round(100 * (eq.iloc[-1] ** (1 / years) - 1), 1) if eq.iloc[-1] > 0 else -100.0,
            "vol": round(100 * r.std() * math.sqrt(DAYS), 1),
            "max_dd": round(float(100 * (eq / eq.cummax() - 1).min()), 1),
            "pos_years": round(100 * sum(v > 0 for v in full_years) / len(full_years)) if full_years else None,
            "carry_pct_yr": round(float(100 * res["carry"].mean() * DAYS), 1),
            "fees_pct_yr": round(float(100 * res["fees"].mean() * DAYS), 1),
            "btc_corr": round(float(r.corr(btc.reindex(r.index))), 2),
            "yearly": yearly, "days": len(r)}
