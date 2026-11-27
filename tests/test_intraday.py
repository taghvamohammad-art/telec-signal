import numpy as np
import pandas as pd

from app.intraday.noise import BARS, BAR, DAY, Params, noise_day, run_halfhour, run_noise, to_days


def frame(days=40, seed=0, drift_day=None):
    rng = np.random.default_rng(seed)
    n = days * BARS
    r = rng.normal(0, 0.002, n)
    if drift_day is not None:
        r[drift_day * BARS:(drift_day + 1) * BARS] += 0.003
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate([[100], c[:-1]])
    idx = np.arange(n) * BAR + 10 * DAY
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0005, "low": np.minimum(o, c) * 0.9995,
                         "close": c, "volume": 1.0}, index=idx)


def test_to_days_drops_incomplete():
    df = frame(5).iloc[:-3]
    days, m = to_days(df)
    assert len(days) == 4 and m["close"].shape == (4, BARS)


def test_trend_day_goes_long_and_profits():
    o = np.full(BARS, 100.0)
    c = 100 * (1 + 0.001 * np.arange(1, BARS + 1))
    o[1:] = c[:-1]
    up, lo, vw = np.full(BARS, 100.5), np.full(BARS, 99.5), np.full(BARS, 100.0)
    r, n = noise_day(o, c, up, lo, vw, 2, 0.0)
    assert r > 0.05 and n == 2  # one entry, one exit at the close


def test_causal_future_days_do_not_change_past():
    a = frame(40, seed=1)
    b = a.copy()
    cut = 30 * BARS
    b.iloc[cut:, :4] = b.iloc[cut:, :4] * np.linspace(1, 1.5, len(b) - cut)[:, None]
    ra, rb = run_noise(a), run_noise(b)
    past = ra.index[ra.index < a.index[cut]]
    assert len(past) > 5
    pd.testing.assert_frame_equal(ra.loc[past], rb.loc[past])
    ha, hb = run_halfhour(a), run_halfhour(b)
    pd.testing.assert_frame_equal(ha.loc[ha.index < a.index[cut]], hb.loc[hb.index < a.index[cut]])


def test_costs_reduce_returns():
    df = frame(40, seed=2)
    free, paid = run_noise(df, Params(cost=0)), run_noise(df, Params(cost=0.001))
    assert paid["ret"].sum() < free["ret"].sum()
