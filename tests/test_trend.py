import numpy as np
import pandas as pd
import pytest

from app.trend.ensemble import Params, coin_exposure, run, slice_positions

DAY = 86_400_000


def daily(close, start=1_600_000_000_000 - 1_600_000_000_000 % DAY, spread=0.01, qv=1e8):
    close = np.asarray(close, dtype=float)
    idx = start + np.arange(len(close)) * DAY
    return pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread),
                         "close": close, "quote_volume": np.full(len(close), qv)}, index=idx)


def test_slice_enters_on_breakout_and_exits_below_trailing_mid():
    c = np.array([10.0] * 10 + [11, 12, 13, 14, 15, 14.5, 14, 13, 12.5, 12, 11])
    df = daily(c, spread=0.0)
    pos = slice_positions(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), 5)
    assert pos[9] == 0 and pos[10] == 1  # 11 > highest high of the previous 5 days (10)
    first_out = int(np.argmax((pos[11:] == 0))) + 11
    # exit only once the close falls below the ratcheted midpoint
    assert 15 < first_out < len(c)


def test_slice_is_causal():
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.03, 500)))
    df = daily(c)
    full = slice_positions(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), 20)
    for cut in (100, 250, 400):
        part = slice_positions(df["high"].to_numpy()[:cut], df["low"].to_numpy()[:cut], df["close"].to_numpy()[:cut], 20)
        assert np.array_equal(full[:cut], part)


def test_short_mirror():
    c = np.array([10.0] * 10 + [9, 8, 7, 6, 7.5, 8.5])
    df = daily(c, spread=0.0)
    pos = slice_positions(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), 5, side=-1)
    assert pos[10] == 1 and pos[-1] == 0


def test_exposure_is_share_of_slices():
    c = np.concatenate([np.full(400, 100.0), np.linspace(100, 300, 200)])
    e, entries = coin_exposure(daily(c))
    assert e.iloc[-1] == pytest.approx(1.0) and e.iloc[100] == 0.0 and entries >= 9


def test_decision_applies_to_next_day_and_costs():
    # flat for a year, then a strong uptrend
    up = np.concatenate([np.full(400, 100.0) * (1 + 0.001 * np.sin(np.arange(400))), 100 * 1.01 ** np.arange(1, 201)])
    flat = np.full(600, 50.0) * (1 + 0.001 * np.sin(np.arange(600)))
    data = {"BTCUSDT": daily(up), "FLATUSDT": daily(flat)}
    r = run(data, {}, Params(top_n=2, cost=0.0))
    r_cost = run(data, {}, Params(top_n=2, cost=0.01))
    assert r.stats["total"] > 0 and r.stats["total"] > r_cost.stats["total"]
    assert r.weights.abs().sum(axis=1).max() <= 1.0 + 1e-9
    # the day a weight first appears, it has not yet earned a return
    w = r.weights["BTCUSDT"]
    first = w[w > 0].index[0]
    assert r.equity.loc[first] == pytest.approx(1.0, abs=1e-9)


def test_funding_charged_to_longs():
    up = 100 * 1.005 ** np.arange(600)
    data = {"BTCUSDT": daily(up, spread=0.001)}
    no_f = run(data, {"BTCUSDT": pd.Series(0.0, index=data["BTCUSDT"].index)}, Params(top_n=1, cost=0.0))
    hi_f = run(data, {"BTCUSDT": pd.Series(0.003, index=data["BTCUSDT"].index)}, Params(top_n=1, cost=0.0))
    assert hi_f.stats["total"] < no_f.stats["total"]
