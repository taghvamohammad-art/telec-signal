import asyncio

import pytest

from app.core.models import Candle, UniverseEntry
from app.core.timeframes import align, expected_open_times, last_closed_open_time, tf_ms
from app.data.backfill import sync_symbol_tf
from app.data.candles import clean_batch, find_gaps, parse_binance_rest_kline, parse_binance_ws_kline, validate_candle
from app.data.stream import chunk, combined_url, stream_names
from app.data.universe import binance_candidates, build_universe, lbank_index, normalize_base, select_analysis_set

M15 = tf_ms("15m")
T0 = 1_764_000_000_000 - (1_764_000_000_000 % M15)  # an aligned 15m open time


def mk(t, o=100.0, h=101.0, l=99.0, c=100.5, tf="15m", sym="BTCUSDT"):
    return Candle(sym, tf, t, o, h, l, c, 10.0, 1000.0, 5)


# ---- timeframes -------------------------------------------------------
def test_align_and_last_closed():
    assert align(T0 + 123_456, "15m") == T0
    assert last_closed_open_time(T0 + 1, "15m") == T0 - M15
    assert last_closed_open_time(T0, "15m") == T0 - M15


def test_expected_open_times_inclusive():
    assert expected_open_times(T0, T0 + 2 * M15, "15m") == [T0, T0 + M15, T0 + 2 * M15]
    assert expected_open_times(T0 + 1, T0 + M15, "15m") == [T0 + M15]


# ---- validation -------------------------------------------------------
def test_unclosed_candle_rejected():
    c = mk(T0)
    assert "not_closed" in validate_candle(c, now_ms=T0 + M15 - 1)
    assert validate_candle(c, now_ms=T0 + M15) == []


def test_bad_ohlc_and_alignment():
    assert "inconsistent_ohlc" in validate_candle(mk(T0, h=99.5), now_ms=T0 + 10 * M15)
    assert "misaligned_open_time" in validate_candle(mk(T0 + 1000), now_ms=T0 + 10 * M15)
    assert "non_finite" in validate_candle(mk(T0, c=float("nan")), now_ms=T0 + 10 * M15)


def test_clean_batch_dedup_last_wins_and_sorted():
    a, b, a2 = mk(T0 + M15), mk(T0), mk(T0 + M15, c=100.9)
    acc, rej = clean_batch([a, b, a2, mk(T0 + 99 * M15)], now_ms=T0 + 5 * M15)
    assert [c.open_time for c in acc] == [T0, T0 + M15]
    assert acc[1].close == 100.9
    assert len(rej) == 1 and rej[0][1] == ["not_closed"]


def test_find_gaps():
    have = [T0, T0 + M15, T0 + 4 * M15, T0 + 6 * M15]
    assert find_gaps(have, "15m", T0, T0 + 6 * M15) == [(T0 + 2 * M15, T0 + 3 * M15), (T0 + 5 * M15, T0 + 5 * M15)]
    assert find_gaps(have, "15m", T0, T0 + M15) == []
    assert find_gaps([], "15m", T0, T0 + M15) == [(T0, T0 + M15)]


# ---- parsing ----------------------------------------------------------
def test_parse_rest_kline():
    row = [T0, "1.0", "2.0", "0.5", "1.5", "100", T0 + M15 - 1, "150", 42, "0", "0", "0"]
    c = parse_binance_rest_kline("ETHUSDT", "15m", row)
    assert (c.open, c.high, c.low, c.close, c.quote_volume, c.trades) == (1.0, 2.0, 0.5, 1.5, 150.0, 42)
    assert c.close_time == T0 + M15 - 1


def test_parse_ws_kline_combined_envelope():
    msg = {"stream": "btcusdt@kline_15m", "data": {"e": "kline", "k": {
        "t": T0, "s": "BTCUSDT", "i": "15m", "o": "1", "h": "2", "l": "0.5", "c": "1.5",
        "v": "10", "q": "15", "n": 3, "x": True}}}
    c, closed = parse_binance_ws_kline(msg)
    assert closed and c.symbol == "BTCUSDT" and c.tf == "15m" and c.open_time == T0


# ---- stream helpers ---------------------------------------------------
def test_stream_url_uses_market_path():
    names = stream_names(["BTCUSDT", "ETHUSDT"], ["15m", "1h"])
    assert names == ["btcusdt@kline_15m", "btcusdt@kline_1h", "ethusdt@kline_15m", "ethusdt@kline_1h"]
    url = combined_url("wss://fstream.binance.com/market/", names[:2])
    assert url == "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_15m/btcusdt@kline_1h"
    assert [len(c) for c in chunk(list(range(320)), 150)] == [150, 150, 20]


# ---- universe ---------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1000PEPE", ("PEPE", 1000)), ("1MBABYDOGE", ("BABYDOGE", 1_000_000)),
    ("1000000MOG", ("MOG", 1_000_000)), ("BTC", ("BTC", 1)), ("1INCH", ("1INCH", 1)),
])
def test_normalize_base(raw, expected):
    assert normalize_base(raw) == expected


def _info(*specs):
    return {"symbols": [
        {"symbol": s, "baseAsset": b, "quoteAsset": "USDT", "contractType": ct, "status": st, "underlyingType": ut}
        for s, b, ct, st, ut in specs]}


def test_binance_candidates_filters():
    info = _info(
        ("BTCUSDT", "BTC", "PERPETUAL", "TRADING", "COIN"),
        ("1000PEPEUSDT", "1000PEPE", "PERPETUAL", "TRADING", "COIN"),
        ("USDCUSDT", "USDC", "PERPETUAL", "TRADING", "COIN"),          # stablecoin
        ("ETHUSDT_260327", "ETH", "CURRENT_QUARTER", "TRADING", "COIN"),  # dated
        ("XAUUSDT", "XAU", "PERPETUAL", "TRADING", "TRADIFI"),          # not crypto
        ("OLDUSDT", "OLD", "PERPETUAL", "SETTLING", "COIN"),            # not trading
    )
    tickers = [{"symbol": s, "quoteVolume": "1000", "highPrice": "110", "lowPrice": "100"}
               for s in ("BTCUSDT", "1000PEPEUSDT", "USDCUSDT", "XAUUSDT", "OLDUSDT")]
    out = binance_candidates(info, tickers)
    assert {c["symbol"] for c in out} == {"BTCUSDT", "1000PEPEUSDT"}
    pepe = next(c for c in out if c["symbol"] == "1000PEPEUSDT")
    assert pepe["base"] == "PEPE" and pepe["multiplier"] == 1000 and round(pepe["range_pct"], 6) == 10.0


def test_build_universe_requires_lbank_and_ranks_by_volume():
    cands = [
        {"symbol": "AUSDT", "base": "A", "multiplier": 1, "quote_volume": 5.0, "range_pct": 1.0},
        {"symbol": "BUSDT", "base": "B", "multiplier": 1, "quote_volume": 9.0, "range_pct": 1.0},
        {"symbol": "CUSDT", "base": "C", "multiplier": 1, "quote_volume": 7.0, "range_pct": 1.0},
    ]
    lb = lbank_index({
        "A/USDT:USDT": {"swap": True, "quote": "USDT", "base": "A"},
        "C/USDT:USDT": {"swap": True, "quote": "USDT", "base": "C"},
        "B/USDT": {"swap": False, "quote": "USDT", "base": "B"},  # spot only
    })
    u = build_universe(cands, lb, size=10, require_lbank=True)
    assert [e.symbol for e in u] == ["CUSDT", "AUSDT"]
    assert u[0].lbank_symbol == "C/USDT:USDT"
    assert len(build_universe(cands, lb, size=2, require_lbank=False)) == 2


def test_select_analysis_set_keeps_always_and_size():
    uni = [UniverseEntry(f"S{i}USDT", f"S{i}", 1, 10.0 ** (9 - i / 5), float(i), True, None) for i in range(30)]
    uni.append(UniverseEntry("BTCUSDT", "BTC", 1, 1.0, 0.1, True, None))
    pick = select_analysis_set(uni, 10, ["BTCUSDT", "NOPEUSDT"])
    assert len(pick) == 10 and pick[0] == "BTCUSDT" and len(set(pick)) == 10


# ---- backfill with fakes ---------------------------------------------
class FakeClient:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    async def klines_range(self, symbol, tf, start, end):
        self.calls.append((start, end))
        return [c for c in self.candles if start <= c.open_time <= end]


class FakeStore:
    def __init__(self, existing=()):
        self.rows = {c.open_time: c for c in existing}
        self.issues = []

    async def last_open_time(self, symbol, tf):
        return max(self.rows) if self.rows else None

    async def upsert_candles(self, cs):
        for c in cs:
            self.rows[c.open_time] = c
        return len(cs)

    async def open_times(self, symbol, tf, start, end):
        return [t for t in self.rows if start <= t <= end]

    async def log_issue(self, kind, symbol=None, tf=None, detail=None):
        self.issues.append(kind)


def test_backfill_fills_tail_and_repairs_hole_without_future_candle():
    now = T0 + 20 * M15 + 5  # candle at T0+20*M15 is still open
    exchange = [mk(T0 + i * M15) for i in range(0, 21)]
    existing = [mk(T0 + i * M15) for i in range(0, 10) if i != 4]  # hole at 4
    store, client = FakeStore(existing), FakeClient(exchange)
    res = asyncio.run(sync_symbol_tf(client, store, "BTCUSDT", "15m", now, backfill_days=1))
    assert sorted(store.rows) == [T0 + i * M15 for i in range(0, 20)]  # 0..19, no open candle 20
    assert res.gaps_left == [] and "gap_unfilled" not in store.issues


def test_backfill_records_unfillable_gap():
    now = T0 + 6 * M15 + 5
    exchange = [mk(T0 + i * M15) for i in range(0, 6) if i != 2]  # exchange itself lacks 2
    store, client = FakeStore([mk(T0)]), FakeClient(exchange)
    res = asyncio.run(sync_symbol_tf(client, store, "BTCUSDT", "15m", now, backfill_days=1))
    assert res.gaps_left == [(T0 + 2 * M15, T0 + 2 * M15)]
    assert "gap_unfilled" in store.issues


def test_select_analysis_set_volume_only_by_default():
    uni = [
        UniverseEntry("BIGUSDT", "BIG", 1, 1e9, 1.0, True, None),
        UniverseEntry("PUMPXUSDT", "PUMPX", 1, 1e7, 40.0, True, None),
        UniverseEntry("MIDUSDT", "MID", 1, 1e8, 2.0, True, None),
    ]
    assert select_analysis_set(uni, 2, []) == ["BIGUSDT", "MIDUSDT"]
    assert "PUMPXUSDT" in select_analysis_set(uni, 2, [], volatility_weight=0.9)
