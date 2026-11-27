import pytest

from app.analysis.lbank_check import Limits, assess, book_metrics, multiplier_of

L = Limits(max_diff_pct=0.5, max_spread_pct=0.2, min_depth_usdt=1000)


def book(bid, ask, qty=100.0, levels=5, step=0.0005):
    return {"bids": [[bid * (1 - i * step), qty] for i in range(levels)],
            "asks": [[ask * (1 + i * step), qty] for i in range(levels)]}


def test_multiplier_from_unified_symbol():
    assert multiplier_of("PEPE/USDT:USDT") == 1
    assert multiplier_of("1000PEPE/USDT:USDT") == 1000


def test_book_metrics():
    mid, spread, depth = book_metrics(book(99.95, 100.05, qty=10), 0.5)
    assert mid == pytest.approx(100) and spread == pytest.approx(0.1)
    assert depth == pytest.approx(sum(99.95 * (1 - i * 0.0005) * 10 for i in range(5)))


def test_levels_shifted_by_price_difference():
    # LBank trades 0.2 above Binance: every level moves up by 0.2, R distances unchanged
    r = assess("SOL/USDT:USDT", book(100.15, 100.25), 100.0, 1, 1, 100.0, 96.0, 106.0, 112.0, L)
    assert r.ok and r.diff_pct == pytest.approx(0.2)
    assert (r.entry, r.stop, r.tp1, r.tp2) == pytest.approx((100.2, 96.2, 106.2, 112.2))


def test_multiplier_scaling_binance_1000_vs_lbank_plain():
    # Binance 1000PEPE at 9.30 == LBank PEPE at 0.00930
    r = assess("PEPE/USDT:USDT", book(0.0092995, 0.0093005, qty=1e7), 9.30, 1000, 1, 9.30, 9.10, 9.60, 9.90, L)
    assert r.ok and abs(r.diff_pct) < 0.01
    assert r.stop == pytest.approx(0.00910, rel=1e-3) and r.tp2 == pytest.approx(0.00990, rel=1e-3)


@pytest.mark.parametrize("bk,price,reason", [
    (book(101.0, 101.1), 100.0, "lbank_price_diff"),
    (book(99.8, 100.2), 100.0, "lbank_spread"),
    (book(99.98, 100.02, qty=0.5), 100.0, "lbank_thin_book"),
])
def test_rejections(bk, price, reason):
    r = assess("SOL/USDT:USDT", bk, price, 1, 1, 100.0, 96.0, 106.0, 112.0, L)
    assert not r.ok and r.reason == reason


def test_not_on_lbank_and_empty_book():
    assert assess(None, None, 100, 1, 1, 100, 96, 106, 112, L).reason == "not_on_lbank"
    assert assess("X/USDT:USDT", {"bids": [], "asks": []}, 100, 1, 1, 100, 96, 106, 112, L).reason == "empty_lbank_book"


def test_price_already_past_tp1_on_lbank():
    # short: LBank already at TP1 even though Binance close was at entry
    r = assess("SOL/USDT:USDT", book(99.95, 100.05), 100.0, 1, -1, 100.0, 104.0, 100.1, 92.0, Limits(5, 1, 0))
    assert not r.ok and r.reason == "lbank_price_past_levels"


def test_contract_size_multiplies_depth():
    thin = assess("SOL/USDT:USDT", book(99.98, 100.02, qty=0.5), 100.0, 1, 1, 100, 96, 106, 112, L, contract_size=1.0)
    deep = assess("SOL/USDT:USDT", book(99.98, 100.02, qty=0.5), 100.0, 1, 1, 100, 96, 106, 112, L, contract_size=100.0)
    assert not thin.ok and deep.ok
