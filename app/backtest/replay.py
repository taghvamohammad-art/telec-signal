"""Replay the live decision logic bar by bar over stored history.

Indicators are computed once on the full history. This is valid because every
indicator is causal (tests/test_analysis.py::test_indicators_are_causal): the
value at bar t is the same whether or not later bars exist. At each 15m close
the frames are cut to bars closed at that moment and passed to the same
evaluate() the live engine uses.
"""

from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

from app.analysis.analyzer import MIN_BARS, evaluate
from app.analysis.regime import classify
from app.core.timeframes import tf_ms

TFS = ("15m", "1h", "4h", "1d")
COOLDOWN_MS = 4 * 3_600_000


class Cutter:
    """Slices precomputed frames to the bars closed at a given moment."""

    def __init__(self, prep: dict[str, pd.DataFrame]):
        self.prep = prep
        self.close_times = {tf: prep[tf].index.to_numpy(dtype=np.int64) + tf_ms(tf) for tf in TFS}

    def at(self, now_ms: int) -> dict[str, pd.DataFrame] | None:
        out = {}
        for tf in TFS:
            k = int(np.searchsorted(self.close_times[tf], now_ms, side="right"))
            if k < MIN_BARS[tf]:
                return None
            out[tf] = self.prep[tf].iloc[:k]
        return out


def btc_ctx_at(btc: Cutter | None, now_ms: int):
    if btc is None:
        return None, None
    cut = btc.at(now_ms)
    if cut is None:
        return None, None
    m = cut["15m"]
    atr = m["atr"].iloc[-1]
    move = (m["close"].iloc[-1] - m["close"].iloc[-5]) / atr if atr and atr > 0 else None
    return classify(cut["1h"]).value, move


def funding_at(series: pd.Series | None, now_ms: int) -> float | None:
    if series is None or series.empty:
        return None
    k = int(np.searchsorted(series.index.to_numpy(dtype=np.int64), now_ms, side="right"))
    return float(series.iloc[k - 1]) if k > 0 else None


def replay_symbol(
    symbol: str,
    prep: dict[str, pd.DataFrame],
    btc_prep: dict[str, pd.DataFrame] | None,
    funding: pd.Series | None,
    start_ms: int,
    end_ms: int,
    weights: dict[str, float] | None = None,
) -> tuple[list[dict], dict]:
    """Returns (candidates, counters). Candidate dicts mirror signal_candidates rows."""
    me = Cutter(prep)
    btc = me if symbol == "BTCUSDT" else (Cutter(btc_prep) if btc_prep else None)
    step = tf_ms("15m")
    opens = prep["15m"].index.to_numpy(dtype=np.int64)
    out: list[dict] = []
    counters: dict[str, int] = {}
    last_signal = -10**18
    for o in opens:
        now = int(o) + step
        if now < start_ms or now > end_ms:
            continue
        cut = me.at(now)
        if cut is None:
            counters["insufficient_data"] = counters.get("insufficient_data", 0) + 1
            continue
        cand, diag = evaluate(symbol, cut, lambda n=now: btc_ctx_at(btc, n), funding_at(funding, now), weights)
        key = diag.result if not diag.result.startswith("score_") else "below_threshold"
        counters[key] = counters.get(key, 0) + 1
        if cand is None or now - last_signal < COOLDOWN_MS:
            continue
        last_signal = now
        out.append({
            "symbol": symbol, "bar_time": int(o), "side": cand.side, "score": cand.score,
            "entry": cand.entry, "stop": cand.stop, "tp1": cand.tp1, "tp2": cand.tp2, "risk": cand.risk,
            "regime_1h": diag.regime_1h, "bias_4h": diag.bias, "f_market": cand.f_market,
            "styles": [asdict(v) for v in cand.votes],
        })
    return out, counters
