import numpy as np
import pandas as pd
import pytest

from app.analysis.analyzer import analyze, closed_only
from app.analysis.indicators import add_indicators, atr, confirmed_pivots, ema, rsi
from app.analysis.regime import Regime, classify
from app.analysis.scoring import THRESHOLD, build_candidate, market_factor
from app.analysis.strategies import Context, Vote, liquidity_sweep, vwap_volume
from app.core.timeframes import tf_ms

M15, H1 = tf_ms("15m"), tf_ms("1h")
T0 = 1_760_000_400_000 - (1_760_000_400_000 % 86_400_000)  # a UTC midnight


def frame(close, tf="15m", start=T0, vol=None, spread=0.002, seed=0):
    close = np.asarray(close, dtype=float)
    rng = np.random.default_rng(seed)
    openp = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(openp, close) * (1 + spread * rng.random(len(close)))
    lo = np.minimum(openp, close) * (1 - spread * rng.random(len(close)))
    v = np.full(len(close), 100.0) if vol is None else np.asarray(vol, dtype=float)
    idx = start + np.arange(len(close)) * tf_ms(tf)
    return pd.DataFrame({"open": openp, "high": hi, "low": lo, "close": close, "volume": v}, index=idx)


def random_walk(n, seed=1, drift=0.0):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(drift, 0.004, n)))


# ---- indicators -------------------------------------------------------
def test_indicators_are_causal():
    """Values for bar t must not change when later bars are added (no look-ahead)."""
    df = frame(random_walk(500), vol=np.random.default_rng(3).uniform(50, 150, 500))
    full = add_indicators(df)
    for cut in (230, 333, 480):
        part = add_indicators(df.iloc[:cut])
        a, b = full.iloc[:cut], part
        for col in b.columns:
            np.testing.assert_allclose(a[col].to_numpy(), b[col].to_numpy(), rtol=1e-9, atol=1e-9, equal_nan=True, err_msg=col)


def test_rsi_bounds_and_known_cases():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(up).dropna().eq(100).all()
    r = rsi(pd.Series(random_walk(300))).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_ema_and_atr_basic():
    s = pd.Series([1.0] * 30)
    assert ema(s, 10).dropna().eq(1.0).all()
    df = frame([100.0] * 40, spread=0.0)
    assert atr(df).dropna().eq(0).all()


def test_pivot_reported_only_after_confirmation():
    lows = [10, 9, 8, 7, 8, 9, 10, 11, 12]
    df = pd.DataFrame({"high": [x + 1 for x in lows], "low": lows}, index=range(len(lows)))
    p = confirmed_pivots(df, k=3)
    assert np.isnan(p["pl"].iloc[3])  # pivot bar itself: not yet known
    assert p["pl"].iloc[6] == 7 and p["pl_pos"].iloc[6] == 3  # confirmed 3 bars later
    assert p["last_pl"].iloc[8] == 7


# ---- regime -----------------------------------------------------------
def test_regime_trend_and_chaos():
    up = add_indicators(frame(100 * np.exp(np.linspace(0, 0.5, 300)) * (1 + 0.001 * np.sin(np.arange(300))), tf="1h"))
    assert classify(up) == Regime.TREND_UP
    down = add_indicators(frame(100 * np.exp(np.linspace(0, -0.5, 300)) * (1 + 0.001 * np.sin(np.arange(300))), tf="1h"))
    assert classify(down) == Regime.TREND_DOWN
    calm = list(random_walk(299, seed=5))
    spike = add_indicators(frame(calm + [calm[-1] * 1.25], tf="1h"))
    assert classify(spike) == Regime.CHAOTIC


# ---- strategies -------------------------------------------------------
def _ctx(m15, h1=None, regime=Regime.RANGE, bias=0):
    h1 = h1 if h1 is not None else m15
    return Context("XUSDT", m15, h1, h1, h1, regime, bias)


def test_liquidity_sweep_long():
    base = 100 + np.sin(np.arange(160) / 4)  # swings with clear lows
    df = frame(base, spread=0.0005)
    ind = add_indicators(df)
    level = ind["last_pl"].iloc[-1]
    # last bar: wicks below the last confirmed low, closes back above, high volume
    df.loc[df.index[-1], ["open", "low", "close", "high", "volume"]] = [level + 0.05, level - 0.4, level + 0.3, level + 0.35, 400]
    v = liquidity_sweep(_ctx(add_indicators(df)))
    assert v is not None and v.side == 1 and v.stop < level - 0.4 and 0 < v.s <= 1


def test_liquidity_sweep_needs_volume():
    base = 100 + np.sin(np.arange(160) / 4)
    df = frame(base, spread=0.0005)
    level = add_indicators(df)["last_pl"].iloc[-1]
    df.loc[df.index[-1], ["open", "low", "close", "high", "volume"]] = [level + 0.05, level - 0.4, level + 0.3, level + 0.35, 100]
    assert liquidity_sweep(_ctx(add_indicators(df))) is None


def test_vwap_filter_is_not_core():
    df = add_indicators(frame(np.linspace(100, 110, 200)))
    v = vwap_volume(_ctx(df))
    assert v is not None and v.side == 1 and v.core is False


# ---- scoring ----------------------------------------------------------
def V(style, side, s, stop, core=None):
    from app.analysis.strategies import STYLE_ROLE
    role = STYLE_ROLE.get(style, "anchor")
    return Vote(style, side, s, stop, "", role == "anchor", role)


def test_anchor_needed_and_confirmation_counts():
    alone = [V("breakout", 1, 0.9, 95)]
    assert build_candidate("X", alone, 100, 3, 1.0) == (None, "not_enough_confirmation")
    confirms_only = [V("liquidity_sweep", 1, 0.9, 95), V("vwap_volume", 1, 0.9, None)]
    assert build_candidate("X", confirms_only, 100, 3, 1.0) == (None, "no_anchor")
    with_confirm = [V("breakout", 1, 0.9, 95), V("vwap_volume", 1, 0.9, None)]
    assert build_candidate("X", with_confirm, 100, 3, 1.0)[1] == "ok"


def test_info_styles_are_ignored():
    votes = [V("breakout", 1, 0.9, 97), V("divergence", 1, 0.9, 96), V("mean_reversion", -1, 0.95, 104)]
    assert build_candidate("X", votes, 100, 3, 1.0) == (None, "not_enough_confirmation")


def test_conflict_blocks_signal():
    votes = [V("breakout", 1, 0.8, 95), V("trend_pullback", 1, 0.8, 96), V("htf_momentum", -1, 0.7, 104)]
    assert build_candidate("X", votes, 100, 3, 1.0) == (None, "conflict")
    info_only_opposite = [V("breakout", 1, 0.8, 95), V("trend_pullback", 1, 0.8, 96), V("divergence", -1, 0.9, 104)]
    assert build_candidate("X", info_only_opposite, 100, 3, 1.0)[1] == "ok"


def test_valid_candidate_levels_in_r():
    votes = [V("breakout", 1, 0.8, 97), V("liquidity_sweep", 1, 0.9, 96), V("vwap_volume", 1, 0.7, None, core=False)]
    c, why = build_candidate("X", votes, 100, 3, 1.0)
    assert why == "ok" and c.score >= THRESHOLD
    assert c.stop == 97 and c.risk == 3  # stop from the anchor style, not the confirmation
    assert c.tp1 == pytest.approx(104.5) and c.tp2 == pytest.approx(109)


def test_tight_stop_widened_and_far_stop_rejected():
    votes = [V("breakout", -1, 0.9, 100.5), V("liquidity_sweep", -1, 0.8, 100.4)]
    c, _ = build_candidate("X", votes, 100, 4, 1.0)
    assert c.risk == 2 and c.stop == 102  # widened to 0.5 ATR
    far = [V("breakout", 1, 0.9, 80), V("liquidity_sweep", 1, 0.8, 85)]
    assert build_candidate("X", far, 100, 4, 1.0) == (None, "stop_too_far")


def test_market_factor_only_lowers():
    assert market_factor(1, "chaotic", 0, None, False)[0] == 0
    assert market_factor(1, "range", -3.5, None, False)[0] == 0
    assert market_factor(1, "trend_down", 0, None, False)[0] == 0.75
    assert market_factor(1, "range", 0, 0.002, False)[0] == 0.7
    assert market_factor(-1, "range", 0, 0.002, False)[0] == 1.0
    assert market_factor(1, "trend_down", -5, None, True)[0] == 1.0  # BTC itself ignores BTC context


def test_low_market_factor_kills_score():
    votes = [V("breakout", 1, 0.8, 97), V("liquidity_sweep", 1, 0.8, 96)]
    assert build_candidate("X", votes, 100, 3, 1.0)[0] is not None
    assert build_candidate("X", votes, 100, 3, 0.7)[0] is None


# ---- analyzer ---------------------------------------------------------
def test_closed_only_drops_open_bar():
    df = frame(random_walk(10), tf="1h")
    now = int(df.index[-1]) + H1 // 2  # last hour still open
    assert len(closed_only(df, "1h", now)) == 9


def test_analyze_runs_end_to_end():
    frames = {
        "15m": frame(random_walk(400, 2), "15m"),
        "1h": frame(random_walk(400, 3), "1h"),
        "4h": frame(random_walk(300, 4), "4h"),
        "1d": frame(random_walk(250, 6), "1d"),
    }
    now = max(int(df.index[-1]) + tf_ms(tf) for tf, df in frames.items())
    cand, diag = analyze("XUSDT", frames, now, btc_frames=frames, funding=0.0001)
    assert diag.result and diag.regime_1h in {r.value for r in Regime}


def test_analyze_insufficient_data():
    frames = {tf: frame(random_walk(20), tf) for tf in ("15m", "1h", "4h", "1d")}
    _, diag = analyze("XUSDT", frames, T0 + 10**10)
    assert diag.result == "insufficient_data"


def test_range_regime_gives_no_trade():
    from app.analysis.analyzer import evaluate
    flat = add_indicators(frame(100 + np.sin(np.arange(400) / 7) * 0.3, tf="1h"))
    prep = {"15m": flat, "1h": flat, "4h": flat, "1d": flat}
    _, d = evaluate("XUSDT", prep, (None, None), regime=Regime.RANGE, bias=0)
    assert d.result == "range"


def test_htf_momentum_fires_once_on_4h_close():
    from app.analysis.strategies import htf_momentum
    n = 120
    base = list(100 + np.sin(np.arange(n - 1) / 5))
    h4 = add_indicators(frame(base + [104.0], tf="4h", spread=0.001))  # last 4h bar breaks the 55-bar high
    d1 = add_indicators(frame(np.linspace(80, 104, 80), tf="1d", start=int(h4.index[0])))
    h1 = add_indicators(frame(base + [104.0], tf="1h", start=int(h4.index[0])))
    close_4h = int(h4.index[-1]) + tf_ms("4h")
    m15 = add_indicators(frame([100.0] * 50 + [104.0], tf="15m", start=close_4h - 51 * M15))
    ctx = Context("XUSDT", m15, h1, h4, d1, Regime.TREND_UP, 1)
    v = htf_momentum(ctx)
    assert v is not None and v.side == 1 and v.stop < 104
    m15_later = add_indicators(frame([100.0] * 51 + [104.0], tf="15m", start=close_4h - 51 * M15))
    assert htf_momentum(Context("XUSDT", m15_later, h1, h4, d1, Regime.TREND_UP, 1)) is None


def test_stop_taken_from_valid_style_when_strongest_is_stale():
    votes = [V("breakout", 1, 0.95, 101.0), V("trend_pullback", 1, 0.8, 97.0)]  # breakout stop already above entry
    c, why = build_candidate("X", votes, 100, 3, 1.0)
    assert why == "ok" and c.stop == 97.0
    stale = [V("breakout", 1, 0.95, 101.0), V("trend_pullback", 1, 0.8, 100.5)]
    assert build_candidate("X", stale, 100, 3, 1.0) == (None, "stop_wrong_side")
