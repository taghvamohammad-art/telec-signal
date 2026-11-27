import json

from app.whales.collector import Rules, aggregate, iter_rows, parse_positions, parse_row, qualifies, select


def row(addr, acct, pnl_all, roi_all, pnl_m, vlm_m):
    return {"ethAddress": addr, "accountValue": str(acct), "displayName": None,
            "windowPerformances": [["day", {"pnl": "1", "roi": "0", "vlm": "0"}],
                                   ["month", {"pnl": str(pnl_m), "roi": "0.1", "vlm": str(vlm_m)}],
                                   ["allTime", {"pnl": str(pnl_all), "roi": str(roi_all), "vlm": "0"}]]}


def test_rules():
    good = parse_row(row("0xA", 1e6, 5e6, 2.0, 1e5, 1e7))
    assert good["address"] == "0xa" and qualifies(good)
    assert not qualifies(parse_row(row("0xb", 5e4, 5e6, 2.0, 1e5, 1e6)))      # small account
    assert not qualifies(parse_row(row("0xc", 1e6, 1e5, 2.0, 1e5, 1e6)))      # not proven
    assert not qualifies(parse_row(row("0xd", 1e6, 5e6, 2.0, -1, 1e6)))       # losing this month
    assert not qualifies(parse_row(row("0xe", 1e6, 5e6, 2.0, 1e5, 1e9)))      # market-making bot


def test_stream_parse_and_select():
    rows = [row(f"0x{i}", 1e6, (i + 1) * 1e6, 1.0, 10, 1e6) for i in range(5)] + [row("0xz", 10, 0, 0, 0, 0)]
    text = json.dumps({"leaderboardRows": rows})
    assert len(list(iter_rows(text))) == 6
    chosen = select(iter_rows(text), Rules(), limit=3)
    assert [w["address"] for w in chosen] == ["0x4", "0x3", "0x2"]


def test_positions_and_flow():
    st = {"marginSummary": {"accountValue": "1000"},
          "assetPositions": [{"position": {"coin": "BTC", "szi": "0.5", "positionValue": "50000", "entryPx": "99000",
                                           "leverage": {"type": "cross", "value": 5}, "unrealizedPnl": "500"}},
                             {"position": {"coin": "ETH", "szi": "-10", "positionValue": "30000", "entryPx": "3100",
                                           "leverage": {"type": "cross", "value": 3}, "unrealizedPnl": "-20"}},
                             {"position": {"coin": "SOL", "szi": "0", "positionValue": "0"}}]}
    acct, ps = parse_positions(st)
    assert acct == 1000 and [p["coin"] for p in ps] == ["BTC", "ETH"] and ps[1]["notional"] == -30000
    f = aggregate({"a": ps, "b": ps[:1]})
    assert f["BTC"]["n_long"] == 2 and f["BTC"]["long_notional"] == 100000
    assert f["ETH"]["n_short"] == 1 and f["ETH"]["short_notional"] == 30000
