import numpy as np
import pandas as pd
import pytest

from app.analysis.analyzer import analyze, btc_context_from_raw, evaluate
from app.analysis.indicators import add_indicators
from app.backtest.portfolio import breakdown, signal_results, simulate_portfolio
from app.backtest.replay import Cutter, btc_ctx_at
from app.core.timeframes import tf_ms
from tests.test_analysis import frame, random_walk

M15 = tf_ms("15m")


def raw_frames(seed):
    # all timeframes cover the same period, like the real database
    start = 1_760_000_000_000 - 1_760_000_000_000 % 86_400_000
    end = start + 200 * 86_400_000
    out = {}
    for i, tf in enumerate(("15m", "1h", "4h", "1d")):
        n = (end - start) // tf_ms(tf)
        out[tf] = frame(random_walk(n, seed + i, drift=0.00005), tf, start=start, seed=seed + i)
    return out


def test_replay_matches_live_analysis():
    """Backtest path (indicators on full history, then cut) == live path (cut, then indicators)."""
    raw, btc_raw = raw_frames(10), raw_frames(50)
    prep = {tf: add_indicators(df) for tf, df in raw.items()}
    btc_prep = {tf: add_indicators(df) for tf, df in btc_raw.items()}
    me, btc = Cutter(prep), Cutter(btc_prep)
    opens = raw["15m"].index.to_numpy()
    checked = 0
    for o in opens[-3000::97]:
        now = int(o) + M15
        cut = me.at(now)
        if cut is None:
            continue
        c1, d1 = evaluate("XUSDT", cut, lambda: btc_ctx_at(btc, now), 0.0001)
        c2, d2 = analyze("XUSDT", raw, now, funding=0.0001, btc_ctx=btc_context_from_raw(btc_raw, now))
        assert (d1.result, d1.regime_1h, d1.bias) == (d2.result, d2.regime_1h, d2.bias)
        assert [(v.style, v.side, v.s) for v in d1.votes] == [(v.style, v.side, v.s) for v in d2.votes]
        assert (c1 is None) == (c2 is None)
        checked += 1
    assert checked >= 25


def _candles(rows, start=0):
    idx = [start + i * M15 for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def C(symbol="AUSDT", t=0, side=1, score=80, entry=100, stop=96, tp1=106, tp2=112):
    return {"symbol": symbol, "bar_time": t, "side": side, "score": score, "entry": entry, "stop": stop,
            "tp1": tp1, "tp2": tp2, "risk": abs(entry - stop), "regime_1h": "range",
            "styles": [{"style": "breakout", "core": True}, {"style": "liquidity_sweep", "core": True}]}


def test_signal_results_in_r():
    candles = {"AUSDT": _candles([[100, 101, 99, 100], [100, 107, 99, 106], [106, 113, 105, 112]])}
    res = signal_results([C()], candles, fee_rate=0.0)
    assert res[0]["exit_reason"] == "tp2"
    assert res[0]["r"] == pytest.approx(0.5 * 1.5 + 0.5 * 3.0)


def test_portfolio_profit_and_limits():
    candles = {
        "AUSDT": _candles([[100, 101, 99, 100], [100, 107, 99, 106], [106, 113, 105, 112], [112, 112, 111, 111]]),
        "BUSDT": _candles([[100, 101, 99, 100], [100, 101, 95, 96], [96, 97, 95, 96], [96, 96, 95, 95]]),
    }
    cands = [C("AUSDT"), C("BUSDT", score=72)]
    out = simulate_portfolio(cands, candles, {}, capital=1000, fee_rate=0.0, max_open=5, max_same_side=1)
    by = {t["symbol"]: t for t in out["trades"]}
    assert by["AUSDT"]["status"] == "closed" and by["AUSDT"]["pnl"] > 0
    assert by["BUSDT"]["status"] == "skipped" and by["BUSDT"]["skip_reason"] == "max same side"
    assert out["final_equity"] == pytest.approx(1000 + by["AUSDT"]["pnl"])
    # 1% risk at score 80 -> about 10 at risk; TP1 half (1.5R) + TP2 half (3R) = 2.25R
    assert by["AUSDT"]["pnl"] == pytest.approx(22.5, rel=0.01)


def test_breakdown_by_style():
    res = [{"status": "closed", "r": 2.0, "styles": [{"style": "a"}, {"style": "b"}]},
           {"status": "closed", "r": -1.0, "styles": [{"style": "a"}]}]
    b = breakdown(res, lambda r: [s["style"] for s in r["styles"]])
    assert b["a"] == {"n": 2, "win_rate": 50.0, "avg_r": 0.5, "total_r": 1.0}
    assert b["b"]["n"] == 1


def trending_frames(seed, days=200):
    """15m candles with alternating trend legs; higher timeframes aggregated from them."""
    rng = np.random.default_rng(seed)
    n = days * 96
    drift = np.repeat(rng.choice([-0.0012, -0.0006, 0.0, 0.0006, 0.0012], size=n // 960 + 1), 960)[:n]
    close = 100 * np.exp(np.cumsum(drift / 4 + rng.normal(0, 0.003, n)))
    vol = rng.lognormal(4, 0.6, n)
    start = 1_760_000_000_000 - 1_760_000_000_000 % 86_400_000
    m15 = frame(close, "15m", start=start, vol=vol, seed=seed)
    out = {"15m": m15}
    for tf in ("1h", "4h", "1d"):
        k = tf_ms(tf) // M15
        g = np.arange(n) // k
        agg = m15.groupby(g).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        agg.index = start + agg.index.to_numpy() * tf_ms(tf)
        out[tf] = agg
    return out


def test_fast_replay_identical():
    """The speed-ups (cached regime/bias, skipping bars where no anchor can fire) change nothing."""
    import app.analysis.scoring as sc
    from app.backtest.replay import replay_symbol
    old = sc.THRESHOLD
    sc.THRESHOLD = 50.0  # a bit looser so synthetic data yields enough signals to compare
    try:
        raw, btc_raw = trending_frames(31), trending_frames(32)
        prep = {tf: add_indicators(df) for tf, df in raw.items()}
        bprep = {tf: add_indicators(df) for tf, df in btc_raw.items()}
        end = int(raw["15m"].index[-1]) + M15
        start = end - 90 * 86_400_000
        slow, _ = replay_symbol("XUSDT", prep, bprep, None, start, end, fast=False)
        fast, _ = replay_symbol("XUSDT", prep, bprep, None, start, end, fast=True)
        assert slow == fast
        assert len(slow) > 0
    finally:
        sc.THRESHOLD = old
