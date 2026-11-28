"""Live target weights of the lab combinations that passed the holdout.

Members are FROZEN as the lab picked them from discovery data on 2026-11-28
(backtest #12); they are not re-picked, so the live test stays honest.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.lab.styles import DAYS, STYLES, Ctx, pnl

COMBOS: dict[str, list[str]] = {
    "combo_all_pos": ["xs_mom_7", "xs_mom_28", "size", "attention", "new_listing", "tsmom_14", "tsmom_60",
                      "tsmom_regime", "btc_ma50", "btc_weekend", "fund_change", "fund_diverge", "high52",
                      "resid_mom", "mom_cond", "tsmom_ens", "squeeze_break", "btc_volman"],
    "combo_top": ["mom_cond", "btc_ma50", "tsmom_14", "size", "attention", "fund_change"],
}
COMBO_FA = {"combo_all_pos": "ترکیب کامل (۱۸ سبک)", "combo_top": "ترکیب منتخب (۶ سبک)"}
TARGET_VOL, MAX_SCALE, VOL_WINDOW = 0.15, 3.0, 60

# family of each style, for the panel
STYLE_KIND = {
    "tsmom_14": "روندی", "tsmom_60": "روندی", "tsmom_regime": "روندی", "tsmom_ens": "روندی", "squeeze_break": "روندی",
    "btc_ma50": "روند بیت‌کوین", "btc_weekend": "تقویمی بیت‌کوین", "btc_volman": "روند بیت‌کوین",
    "xs_mom_7": "مومنتوم مقایسه‌ای", "xs_mom_28": "مومنتوم مقایسه‌ای", "resid_mom": "مومنتوم مقایسه‌ای",
    "mom_cond": "مومنتوم مقایسه‌ای", "high52": "مومنتوم مقایسه‌ای",
    "size": "عامل مقایسه‌ای", "attention": "عامل مقایسه‌ای",
    "fund_change": "فاندینگ", "fund_diverge": "فاندینگ", "new_listing": "ارز تازه‌لیست",
}


def member_weights(c: Ctx, cost: float = 0.001) -> dict[str, tuple[pd.DataFrame, pd.Series]]:
    """Each style used by any combination: (weights, scale) where scale_d is decided with returns up to d."""
    out = {}
    needed = sorted({m for ms in COMBOS.values() for m in ms})
    for key in needed:
        _, fn, hedged = STYLES[key]
        w = fn(c)
        r = pnl(c, w, cost, hedged)["ret"].reindex(c.close.index)
        vol = r.rolling(VOL_WINDOW, min_periods=30).std() * math.sqrt(DAYS)
        scale = (TARGET_VOL / vol).clip(upper=MAX_SCALE).where(vol > 0)
        out[key] = (w, scale)
    return out


def combo_weights(members: dict[str, tuple[pd.DataFrame, pd.Series]], combo: str) -> pd.DataFrame:
    """Equal-risk mix: same as lab.styles.combine, but as positions instead of returns."""
    keys = COMBOS[combo]
    n = pd.concat([members[k][1].notna() for k in keys], axis=1).sum(axis=1)
    total = None
    for k in keys:
        w, s = members[k]
        part = w.mul(s.fillna(0.0), axis=0)
        total = part if total is None else total.add(part, fill_value=0.0)
    return total.div(n.where(n > 0), axis=0).fillna(0.0)


def contributions(members: dict[str, tuple[pd.DataFrame, pd.Series]], combo: str, day: int,
                  symbols: list[str]) -> dict[str, list[list]]:
    """Which styles make up each position on `day`: [label, kind, side, share %], largest first."""
    keys = COMBOS[combo]
    n = sum(1 for k in keys if pd.notna(members[k][1].get(day)))
    if n == 0:
        return {}
    parts: dict[str, list] = {s: [] for s in symbols}
    for k in keys:
        w, sc = members[k]
        scale = sc.get(day)
        if scale is None or scale != scale or day not in w.index:
            continue
        row = w.loc[day]
        for s in symbols:
            v = row.get(s, 0.0)
            if v and v == v:
                parts[s].append((k, v * scale / n))
    out = {}
    for s, items in parts.items():
        tot = sum(abs(v) for _, v in items)
        if tot <= 0:
            continue
        items.sort(key=lambda kv: -abs(kv[1]))
        out[s] = [[STYLES[k][0], STYLE_KIND.get(k, ""), 1 if v > 0 else -1, round(100 * abs(v) / tot)] for k, v in items]
    return out
