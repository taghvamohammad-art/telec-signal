"""Confluence scoring and signal construction (v2.1, after the first backtest).

Score (0..100) = 100 x [0.6 x weighted mean s of agreeing styles
                       + 0.4 x min(1, agreeing styles / 3)] x F_market x F_news

Hard rules:
  * at least one ANCHOR style (trend pullback, breakout, 4h momentum) with s >= 0.6
  * at least MIN_AGREE agreeing anchor/confirm styles in total
  * a strong anchor pointing the other way blocks the signal (conflict)
  * info styles (divergence, mean reversion) never affect the decision
  * stop distance between 0.5 and 3 ATR(1h); a tighter stop is widened to 0.5 ATR
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.strategies import Vote

VERSION = "s2.1"
MIN_AGREE = 2
STRONG = 0.6
THRESHOLD = 70.0
TP1_R, TP2_R = 1.5, 3.0
DEFAULT_WEIGHTS = {
    "trend_pullback": 1.0,
    "breakout": 1.0,
    "mean_reversion": 1.0,
    "liquidity_sweep": 1.0,
    "divergence": 1.0,
    "vwap_volume": 0.5,
    "htf_momentum": 1.0,
}


@dataclass
class Candidate:
    symbol: str
    side: int
    score: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk: float
    votes: list[Vote]
    f_market: float
    f_news: float
    notes: list[str] = field(default_factory=list)


def market_factor(side: int, btc_regime_1h: str | None, btc_move_atr: float | None, funding: float | None, is_btc: bool) -> tuple[float, list[str]]:
    """BTC context + funding crowding. Each part only lowers the score."""
    f, notes = 1.0, []
    if btc_regime_1h == "chaotic":
        return 0.0, ["بازار بیت‌کوین آشفته است"]
    if not is_btc and btc_move_atr is not None and side * btc_move_atr <= -3:
        return 0.0, ["حرکت شدید بیت‌کوین خلاف جهت"]
    if not is_btc and ((side == 1 and btc_regime_1h == "trend_down") or (side == -1 and btc_regime_1h == "trend_up")):
        f *= 0.75
        notes.append("روند بیت‌کوین خلاف جهت")
    if funding is not None:
        crowd = side * funding
        if crowd > 0.001:
            f *= 0.7
            notes.append("فاندینگ بسیار یک‌طرفه")
        elif crowd > 0.0005:
            f *= 0.85
            notes.append("فاندینگ یک‌طرفه")
    return f, notes


def build_candidate(
    symbol: str,
    votes: list[Vote],
    entry: float,
    atr_1h: float,
    f_market: float,
    f_news: float = 1.0,
    weights: dict[str, float] | None = None,
    market_notes: list[str] | None = None,
) -> tuple[Candidate | None, str]:
    """Returns (candidate or None, reason when None)."""
    w = weights or DEFAULT_WEIGHTS
    if not votes:
        return None, "no_votes"
    anchors = [v for v in votes if v.role == "anchor" and v.s >= STRONG]
    sides = {v.side for v in anchors}
    if len(sides) > 1:
        return None, "conflict"
    if not sides:
        return None, "no_anchor"
    side = sides.pop()
    agree = [v for v in votes if v.role != "info" and v.side == side]
    if len(agree) < MIN_AGREE:
        return None, "not_enough_confirmation"
    wsum = sum(w.get(v.style, 1.0) for v in agree)
    wmean = sum(w.get(v.style, 1.0) * v.s for v in agree) / wsum
    raw = 0.6 * wmean + 0.4 * min(1.0, len(agree) / 3)
    score = round(100 * raw * f_market * f_news, 1)
    if score < THRESHOLD:
        return None, f"score_{score}"

    # stop: strongest anchor whose stop is still on the right side, else a confirming style's stop
    valid = [v for v in agree if v.stop is not None and side * (entry - v.stop) > 0]
    if not valid:
        return None, "stop_wrong_side" if any(v.stop is not None for v in agree) else "no_stop"
    valid.sort(key=lambda v: (v.role != "anchor", -v.s))
    stop = valid[0].stop
    risk = side * (entry - stop)
    if risk > 3 * atr_1h:
        return None, "stop_too_far"
    if risk < 0.5 * atr_1h:
        risk = 0.5 * atr_1h
        stop = entry - side * risk
    cand = Candidate(
        symbol=symbol,
        side=side,
        score=score,
        entry=entry,
        stop=stop,
        tp1=entry + side * TP1_R * risk,
        tp2=entry + side * TP2_R * risk,
        risk=risk,
        votes=agree,
        f_market=f_market,
        f_news=f_news,
        notes=list(market_notes or []),
    )
    return cand, "ok"
