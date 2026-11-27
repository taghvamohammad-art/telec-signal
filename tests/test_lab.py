import numpy as np
import pandas as pd

from app.lab.styles import STYLES, build_ctx, deflated_sharpe, pnl

D = 86_400_000


def market(seed=0, n=40, days=500):
    rng = np.random.default_rng(seed)
    daily, fund = {}, {}
    for i in range(n):
        start = 0 if i < 30 else int(rng.integers(100, 400))
        k = days - start
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, k)))
        idx = np.arange(start, days) * D
        name = "BTCUSDT" if i == 0 else f"C{i}USDT"
        daily[name] = pd.DataFrame({"close": c, "quote_volume": rng.uniform(1e6, 1e9, k)}, index=idx)
        fund[name] = pd.Series(rng.normal(0.0003, 0.0003, k), index=idx)
    return daily, fund


def test_all_styles_causal():
    d1, f1 = market(1)
    d2 = {s: df.copy() for s, df in d1.items()}
    cut = 400 * D
    for s in d2:
        d2[s].loc[d2[s].index > cut, "close"] *= np.linspace(1, 4, (d2[s].index > cut).sum())
    c1, c2 = build_ctx(d1, f1), build_ctx(d2, f1)
    for key, (_, fn, hedged) in STYLES.items():
        a, b = pnl(c1, fn(c1), 0.001, hedged), pnl(c2, fn(c2), 0.001, hedged)
        pd.testing.assert_series_equal(a["ret"][a.index <= cut], b["ret"][b.index <= cut], obj=key)


def test_market_neutral_styles_balance():
    d, f = market(2)
    c = build_ctx(d, f)
    for key in ("xs_mom_7", "lowvol", "size", "bab", "max"):
        w = STYLES[key][1](c)
        live = w[w.abs().sum(axis=1) > 0]
        assert np.allclose(live.sum(axis=1), 0, atol=1e-9), key


def test_deflated_sharpe_penalises_many_trials():
    r = pd.Series(np.random.default_rng(0).normal(0.001, 0.02, 1500))
    assert deflated_sharpe(r, 2, 0.0004) > deflated_sharpe(r, 200, 0.0004)


def test_combination_is_causal_and_uses_discovery_only():
    from app.lab.styles import combine, pick_combos

    d, f = market(4)
    c = build_ctx(d, f)
    full = {k: pnl(c, fn(c), 0.001, h) for k, (_, fn, h) in STYLES.items()}
    members = ["tsmom_14", "btc_ma50", "size"]
    a = combine(full, members)
    cut = a.index[len(a) // 2]
    short = {k: v[v.index <= cut] for k, v in full.items()}
    b = combine(short, members)
    pd.testing.assert_series_equal(a["ret"][a.index <= cut], b["ret"])
    picks = pick_combos({k: v[v.index <= cut] for k, v in full.items()}, {"carry_neutral"})
    assert "carry_neutral" not in picks["combo_all_pos"] and len(picks["combo_top"]) <= 6
