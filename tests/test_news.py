from app.news.classify import (category, coins, event_fa, importance, parse_binance_delist, parse_feed, same_story,
                               tokens)

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Exchange &amp; DeFi protocol hacked for $40M in ETH</title><link>https://ex.com/a</link>
<pubDate>Fri, 27 Nov 2026 11:14:28 +0000</pubDate><description><![CDATA[<p>Attackers drained funds</p>]]></description></item>
<item><title>No link item</title></item></channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Fed holds rates steady</title>
<link href="https://fed.gov/x"/><updated>2026-11-27T19:00:00Z</updated><summary>FOMC statement</summary></entry></feed>"""


def test_parse_rss_and_atom():
    a = parse_feed(RSS)
    assert len(a) == 1 and a[0]["title"].startswith("Exchange & DeFi") and a[0]["summary"] == "Attackers drained funds"
    assert a[0]["published"].year == 2026
    b = parse_feed(ATOM)
    assert b[0]["url"] == "https://fed.gov/x" and b[0]["published"].hour == 19


def test_rejects_entity_bombs():
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    try:
        parse_feed(bomb)
        ok = False
    except Exception:
        ok = True
    assert ok


def test_categories():
    assert category("Protocol X hacked for $40M", "", "crypto")[0] == "hack"
    assert category("Binance Will Delist ABC, DEF on 2026-12-05", "", "official")[0] == "delist"
    assert category("Missile strikes hit Kyiv as talks stall", "", "world")[0] == "war"
    assert category("Fed signals rate cut in December", "", "world")[0] == "macro"
    assert category("SEC sues token issuer over unregistered offering", "", "crypto")[0] == "regulation"
    assert category("Bitcoin ETF inflows top $1 billion", "", "crypto")[0] == "etf"
    assert category("Bitcoin climbs above $90,000", "", "crypto")[0] == "market"
    assert category("Banking stocks rally", "", "world")[0] == "world"      # 'ban' must not match 'banking'


def test_coins():
    known = {"BTC", "ETH", "SOL", "ONE", "OP", "ARB", "SEC"}
    assert coins("Solana and Bitcoin rally; ARB jumps", "", known) == ["ARB", "BTC", "SOL"]
    assert coins("SEC says ONE thing about ETFs", "", known) == []        # common words are not tickers


def test_importance_and_stories():
    assert importance(4, ["BTC"], set(), True, False, "Exchange hacked for $1.2 billion") == 5
    assert importance(1, [], set(), False, False, "Bitcoin edges up") == 1
    a = tokens("Bybit hacked: attackers steal $1.4 billion in ETH")
    b = tokens("Hackers steal $1.4 billion in ETH from Bybit exchange")
    c = tokens("Fed holds interest rates steady")
    assert same_story(a, b) and not same_story(a, c)


def test_binance_delist_and_calendar_names():
    data = {"code": "000000", "data": {"catalogs": [{"catalogId": 161, "articles": [
        {"id": 1, "code": "abc123", "title": "Binance Will Delist XYZ on 2026-12-05", "type": 1, "releaseDate": 1764201600000}]}]}}
    items = parse_binance_delist(data)
    assert items[0]["url"].endswith("/abc123") and items[0]["published"].year == 2025 or items[0]["published"].year == 2026
    assert event_fa("Core CPI m/m") == "تورم هسته (CPI)" and event_fa("Non-Farm Employment Change") == "اشتغال غیرکشاورزی"
    assert event_fa("Federal Funds Rate") == "تصمیم نرخ بهرهٔ فدرال‌رزرو" and event_fa("Building Permits") is None
