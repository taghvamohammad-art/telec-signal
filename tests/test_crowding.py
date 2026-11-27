import numpy as np
import pandas as pd

from app.crowding.strategy import TSParams, XSParams, cross_sectional, time_series

D = 86_400_000


def make(n_coins=12, days=400, seed=0):
    rng = np.random.default_rng(seed)
    idx = np.arange(days) * D
    daily, fund = {}, {}
    for i in range(n_coins):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, days)))
        daily[f"C{i}USDT"] = pd.DataFrame({"close": c, "quote_volume": 1e6 * (i + 1)}, index=idx)
        fund[f"C{i}USDT"] = pd.Series(rng.normal(0.0003 * (i - 6), 0.0002, days), index=idx)
    return daily, fund


def test_xs_is_market_neutral_and_shorts_high_funding():
    daily, fund = make()
    res, w = cross_sectional(daily, fund, XSParams(top_n=12, q=0.25, min_history=30))
    live = w[w.abs().sum(axis=1) > 0]
    assert np.allclose(live.sum(axis=1), 0, atol=1e-9) and np.allclose(live.abs().sum(axis=1), 1)
    last = live.iloc[-1]
    assert last["C11USDT"] < 0 and last["C0USDT"] > 0      # highest funding shorted, lowest longed
    assert res["carry"].mean() > 0                          # shorts collect, longs receive negative funding


def test_causal():
    daily, fund = make(seed=3)
    d2 = {s: df.copy() for s, df in daily.items()}
    f2 = {s: f.copy() for s, f in fund.items()}
    cut = 300 * D
    for s in d2:
        d2[s].loc[d2[s].index > cut, "close"] *= 3
        f2[s].loc[f2[s].index > cut] = 0.01
    for fn, p in ((cross_sectional, XSParams(top_n=12, min_history=30)), (time_series, TSParams(top_n=12, min_history=100))):
        a, _ = fn(daily, fund, p)
        b, _ = fn(d2, f2, p)
        pd.testing.assert_series_equal(a["ret"][a.index <= cut], b["ret"][b.index <= cut])


def test_ts_trigger_holds_then_exits():
    daily, fund = make(n_coins=3, days=300, seed=1)
    fund["C0USDT"].iloc[200:203] = 0.02            # a burst of extreme positive funding
    _, w = time_series(daily, fund, TSParams(top_n=3, min_history=100, hold=5))
    col = w["C0USDT"]
    assert (col.iloc[202:207] < 0).all() and col.iloc[212] == 0
