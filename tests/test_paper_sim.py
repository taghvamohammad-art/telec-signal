import pytest

from app.paper.sim import Trade, funding_times, risk_pct_for_score, size_position, stats

FEE = 0.0006


def T(side=1, entry=100.0, stop=96.0, tp1=106.0, tp2=112.0, qty=10.0, fee=0.0):
    t = Trade(side, entry, qty, stop, tp1, tp2, fee)
    t.open_fee(0)
    return t


def test_risk_is_fixed_since_v21():
    assert {risk_pct_for_score(s) for s in (90, 85, 80, 75, 72)} == {1.0}


def test_sizing_risk_matches_target_and_leverage_is_safe():
    s = size_position(equity=1000, free_margin=1000, slots_left=5, score=80, entry=100, stop=96, fee_rate=FEE)
    # 1% of 1000 = 10 at risk including fees
    assert s.risk_amount == pytest.approx(10.0)
    assert s.qty == pytest.approx(10 / (4 + 0.12))
    # liquidation (approx entry/lev) must be farther than 1.5x the stop distance
    assert 100 / s.leverage > 1.5 * 4
    assert s.margin <= 1000 / 5 + 1e-9


def test_sizing_shrinks_when_margin_short():
    s = size_position(equity=1000, free_margin=50, slots_left=5, score=90, entry=100, stop=99.5, fee_rate=FEE)
    assert s.note and s.margin <= 10 + 1e-9 and s.risk_amount < 10


def test_sizing_rejects_bad_input():
    assert size_position(1000, 1000, 0, 80, 100, 96, FEE) is None
    assert size_position(1000, 1000, 5, 80, 100, 100, FEE) is None


def test_long_stop_loss():
    t = T()
    t.on_candle(1, high=101, low=95)
    assert t.closed and t.exit_reason == "stop" and t.realized == pytest.approx(-40)


def test_long_tp1_then_breakeven():
    t = T()
    t.on_candle(1, high=107, low=99)
    assert t.tp1_hit and not t.closed and t.stop == 100 and t.qty_open == 5
    t.on_candle(2, high=105, low=99.5)
    assert t.closed and t.exit_reason == "breakeven"
    assert t.realized == pytest.approx(30)  # +6 x 5, the rest closed flat


def test_long_tp1_and_tp2_same_candle():
    t = T()
    t.on_candle(1, high=113, low=99)
    assert t.closed and t.exit_reason == "tp2" and t.realized == pytest.approx(5 * 6 + 5 * 12)


def test_stop_wins_when_candle_hits_both():
    t = T()
    t.on_candle(1, high=113, low=95)
    assert t.exit_reason == "stop" and t.realized == pytest.approx(-40)


def test_short_mirror():
    t = T(side=-1, entry=100, stop=104, tp1=94, tp2=88)
    t.on_candle(1, high=101, low=93)
    assert t.tp1_hit and t.stop == 100
    t.on_candle(2, high=99, low=87)
    assert t.exit_reason == "tp2" and t.realized == pytest.approx(5 * 6 + 5 * 12)


def test_fees_and_funding():
    t = T(fee=FEE)
    assert t.realized == pytest.approx(-10 * 100 * FEE)
    t.apply_funding(1, rate=0.0001, mark=100)  # long pays
    assert t.funding == pytest.approx(0.1)
    t.on_candle(2, high=101, low=95)
    expected = -40 - 10 * 100 * FEE - 10 * 96 * FEE - 0.1
    assert t.realized == pytest.approx(expected)
    assert t.fees == pytest.approx(10 * 100 * FEE + 10 * 96 * FEE)


def test_short_receives_positive_funding():
    t = T(side=-1, entry=100, stop=104, tp1=94, tp2=88)
    t.apply_funding(1, rate=0.0002, mark=100)
    assert t.funding == pytest.approx(-0.2) and t.realized == pytest.approx(0.2)


def test_funding_times():
    h = 3_600_000
    day = 1_764_201_600_000  # a UTC midnight
    assert funding_times(day - 1, day + 17 * h) == [day, day + 8 * h, day + 16 * h]
    assert funding_times(day, day + 7 * h) == []


def test_stats():
    s = stats([10, -5, 20, -5], [1000, 1010, 1005, 1025, 1020])
    assert s["trades"] == 4 and s["win_rate"] == 50.0 and s["net_pnl"] == 20
    assert s["profit_factor"] == 3.0 and s["expectancy"] == 5
    assert s["max_drawdown_pct"] == pytest.approx(round(100 * 5 / 1010, 2))
    assert stats([], [1000])["win_rate"] is None
