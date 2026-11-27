import pytest

from app.bot import messages as M
from app.signals.lifecycle import FEE, trade_in_r


def test_trade_in_r_full_win():
    t = trade_in_r(1, 100.0, 96.0, 106.0, 112.0, None, False, 0.0)
    t.on_candle(0, 113, 99)
    # half at 1.5R, half at 3R, minus exit fees in R
    fees_r = (0.5 * 106 + 0.5 * 112) * FEE / 4
    assert t.exit_reason == "tp2" and t.realized == pytest.approx(2.25 - fees_r)


def test_trade_in_r_resumes_after_tp1():
    t = trade_in_r(-1, 100.0, 104.0, 94.0, 88.0, 100.0, True, 0.7)
    assert t.qty_open == pytest.approx(0.5 * t.qty) and t.stop == 100.0
    t.on_candle(0, 100.5, 99)  # back to entry: breakeven on the remaining half
    assert t.exit_reason == "breakeven" and t.realized == pytest.approx(0.7 - 0.5 * 100 * FEE / 4)


SIG = {"id": 1, "symbol": "SOLUSDT", "side": -1, "score": 82.4, "entry": 142.35, "stop": 145.1, "tp1": 138.23, "tp2": 134.1,
       "styles": [{"style": "trend_pullback", "role": "anchor"}, {"style": "vwap_volume", "role": "confirm"},
                  {"style": "divergence", "role": "info"}], "notes": [],
       "lbank_entry": 142.45, "lbank_stop": 145.2, "lbank_tp1": 138.33, "lbank_tp2": 134.2}


def test_new_signal_uses_lbank_levels_and_core_styles():
    text = M.new_signal(SIG, "https://signal.telec.shop")
    assert "فروش SOLUSDT" in text and "142.45" in text and "145.20" in text
    assert "پولبک روندی" in text and "VWAP" in text and "واگرایی" not in text
    assert "ریسک پیشنهادی: 1.0٪" in text and "ال‌بانک" in text


def test_new_signal_warns_when_only_binance_prices():
    sig = {**SIG, "lbank_entry": None}
    text = M.new_signal(sig, "https://x")
    assert "142.35" in text and "بایننس" in text and "⚠️" in text


def test_update_texts():
    assert "TP1" in M.update(SIG, "tp1", {"r": 0.75})
    assert "+2.25R" in M.update(SIG, "tp2", {"r": 2.25})
    assert "-1.00R" in M.update(SIG, "stop", {"r": -1.0})
    assert M.update(SIG, "active", {}) is None


def test_symbol_is_escaped():
    text = M.new_signal({**SIG, "symbol": "<b>X</b>"}, "https://x")
    assert "<b>X</b>" not in text.replace("<b>فروش", "")


def test_price_format():
    assert M.fmt_price(59930.12) == "59,930.1"
    assert M.fmt_price(142.346) == "142.35"
    assert M.fmt_price(0.0093412) == "0.009341"
