"""Rule-based news handling: feed parsing, category, coins, importance, and cross-source verification.
No AI: every decision is a keyword rule that can be read and checked."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:  # hardened XML parser for untrusted feeds
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    from xml.etree import ElementTree as ET

# ---- sources -------------------------------------------------------------------------
# group: crypto | world | official. Official sources count as verified on their own.
FEEDS = [
    ("CoinDesk", "crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "crypto", "https://cointelegraph.com/rss"),
    ("The Block", "crypto", "https://www.theblock.co/rss.xml"),
    ("Decrypt", "crypto", "https://decrypt.co/feed"),
    ("CryptoSlate", "crypto", "https://cryptoslate.com/feed/"),
    ("BBC World", "world", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "world", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Al Jazeera", "world", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian", "world", "https://www.theguardian.com/world/rss"),
    ("CNBC Economy", "world", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC World", "world", "https://www.cnbc.com/id/100727362/device/rss/rss.html"),
    ("Federal Reserve", "official", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("SEC", "official", "https://www.sec.gov/news/pressreleases.rss"),
]
BINANCE_DELIST_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                      "?type=1&catalogId=161&pageNo=1&pageSize=20")
CALENDAR_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                 "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]

# ---- categories (first match wins, in this order) -------------------------------------
CATEGORIES = [
    ("hack", "هک و سرقت", 4, r"\b(hack(ed|er|ers)?|exploit(ed)?|drain(ed)?|stolen|breach|heist|rug ?pull|compromised)\b"),
    ("delist", "حذف از صرافی", 3, r"\b(delist(s|ed|ing)?|will remove|removal of|suspend(s|ed)? (trading|deposits|withdrawals))\b"),
    ("war", "سیاست و جنگ", 3, r"\b(war|missile|airstrikes?|strikes? on|invasion|invade[sd]?|troops|ceasefire|military|nuclear|"
                             r"sanction(s|ed)?|bomb(ing|ed)?|drone attack|coup|terror(ist|ism)?|hostage)\b"),
    ("macro", "اقتصاد کلان", 3, r"\b(fed|fomc|federal reserve|interest rates?|rate (cut|hike)s?|inflation|cpi|pce|payrolls?|"
                               r"jobs report|unemployment|recession|gdp|tariffs?|treasury yields?|bond yields?|ecb|central bank|powell)\b"),
    ("regulation", "قانون‌گذاری", 3, r"\b(sec|cftc|regulat(or|ors|ion|ory)|lawsuit|sued|sues|court|ban(s|ned)?|legislation|bill|"
                                    r"stablecoin law|mica|license|enforcement|charged|indict(ed|ment)?)\b"),
    ("etf", "ای‌تی‌اف و نهادی", 2, r"\b(etfs?|inflows?|outflows?|blackrock|fidelity|grayscale|microstrategy|strategy inc|treasury company)\b"),
    ("listing", "لیست شدن", 2, r"\b(will list|lists|listing|launch(es)? (perpetual|futures|trading))\b"),
]
CAT_LABEL = {k: fa for k, fa, _, _ in CATEGORIES} | {"market": "بازار کریپتو", "world": "جهان"}
_CAT_RE = [(k, base, re.compile(rx, re.I)) for k, _, base, rx in CATEGORIES]

NAMES = {
    "bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "ripple": "XRP", "dogecoin": "DOGE",
    "cardano": "ADA", "tron": "TRX", "chainlink": "LINK", "avalanche": "AVAX", "polkadot": "DOT", "litecoin": "LTC",
    "toncoin": "TON", "shiba inu": "SHIB", "uniswap": "UNI", "aave": "AAVE", "hyperliquid": "HYPE", "sui": "SUI",
    "aptos": "APT", "arbitrum": "ARB", "optimism": "OP", "polygon": "POL", "pepe": "PEPE", "binance coin": "BNB",
    "bnb chain": "BNB", "stellar": "XLM", "monero": "XMR", "zcash": "ZEC", "filecoin": "FIL", "cosmos": "ATOM",
    "near protocol": "NEAR", "internet computer": "ICP", "ethena": "ENA", "bittensor": "TAO", "celestia": "TIA",
    "injective": "INJ", "render": "RENDER", "worldcoin": "WLD", "jupiter": "JUP", "ondo": "ONDO", "pendle": "PENDLE",
}
NOT_TICKERS = {"SEC", "CEO", "CFO", "CTO", "ETF", "ETFS", "USD", "US", "UK", "EU", "AI", "IPO", "GDP", "CPI", "PCE", "FED", "FBI",
               "DOJ", "NFT", "NFTS", "DEFI", "TVL", "API", "ATH", "USA", "UN", "NATO", "IMF", "ECB", "BOJ", "BOE", "OPEC", "IRS",
               "CFTC", "LTD", "INC", "THE", "NEW", "FOR", "AND", "NOT", "ALL", "ARE", "CAN", "HAS", "WAS", "NOW", "ONE", "TWO",
               "TOP", "BIG", "WIN", "KEY", "RUN", "SET", "OUT", "UP", "IT", "IS", "IN", "ON", "AT", "TO", "OF", "BY", "AS", "OR",
               "AN", "BE", "DO", "GO", "NO", "SO", "WE", "MY", "ME", "OK", "TV", "PM", "AM", "EV", "Q1", "Q2", "Q3", "Q4",
               "FTX", "DEX", "CEX", "DAO", "RWA", "L1", "L2", "KYC", "AML", "OTC", "P2P", "APY", "APR", "YTD", "UAE", "GPU"}
MAJORS = {"BTC", "ETH"}
_STOP = set("a an the of to in on for and or but is are was were be been with at by from as that this it its into over after "
            "before about amid says said say will would could may might new up down than more most less what why how who "
            "has have had not no yes report reports news live update".split())


# ---- parsing --------------------------------------------------------------------------------
def _text(el) -> str:
    if el is None:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", "".join(el.itertext()))).strip()


def _date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml: bytes | str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, url, published, summary}]."""
    root = ET.fromstring(xml)
    out = []
    items = root.findall(".//item")
    atom = False
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        atom = True
    for it in items:
        if atom:
            ns = "{http://www.w3.org/2005/Atom}"
            title = _text(it.find(f"{ns}title"))
            link_el = it.find(f"{ns}link")
            url = link_el.get("href") if link_el is not None else ""
            pub = _date(_text(it.find(f"{ns}published")) or _text(it.find(f"{ns}updated")))
            summary = _text(it.find(f"{ns}summary"))
        else:
            title = _text(it.find("title"))
            url = _text(it.find("link")) or _text(it.find("guid"))
            pub = _date(_text(it.find("pubDate")) or _text(it.find("{http://purl.org/dc/elements/1.1/}date")))
            summary = _text(it.find("description"))
        if title and url.startswith(("http://", "https://")):
            out.append({"title": title[:400], "url": url[:1000], "published": pub, "summary": re.sub(r"\s+", " ", summary)[:600]})
    return out


def parse_binance_delist(data: dict) -> list[dict]:
    """Binance announcement JSON (delisting catalog) -> items. Walks the JSON for article-like dicts."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if "title" in x and "releaseDate" in x and "code" in x:
                found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    out = []
    for a in found:
        try:
            pub = datetime.fromtimestamp(int(a["releaseDate"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            pub = None
        out.append({"title": str(a["title"])[:400], "summary": "",
                    "url": f"https://www.binance.com/en/support/announcement/{a['code']}", "published": pub})
    return out


# ---- classification ----------------------------------------------------------------------------
def category(title: str, summary: str, group: str) -> tuple[str, int]:
    text = f"{title} . {summary[:300]}"
    for key, base, rx in _CAT_RE:
        if rx.search(title):
            return key, base
    for key, base, rx in _CAT_RE:      # summary only counts for the strong categories
        if key in ("hack", "delist", "war") and rx.search(text):
            return key, base - 1
    return ("market", 1) if group == "crypto" else ("world", 1)


def coins(title: str, summary: str, known: set[str]) -> list[str]:
    found = set()
    low = f" {title.lower()} {summary[:300].lower()} "
    for name, t in NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", low):
            found.add(t)
    for m in re.finditer(r"(?<![A-Za-z0-9$])\$?([A-Z][A-Z0-9]{1,9})(?![a-z])", title):
        t = m.group(1)
        if t in known and t not in NOT_TICKERS:
            found.add(t)
    return sorted(found)


def importance(cat_base: int, coin_list: list[str], held: set[str], verified: bool, official: bool, title: str) -> int:
    score = cat_base
    if set(coin_list) & (held | MAJORS):
        score += 1
    if verified or official:
        score += 1
    if re.search(r"\b(billion|record|crash(es)?|plunge[sd]?|soar(s|ed)?|emergency|breaking)\b", title, re.I):
        score += 1
    return max(1, min(5, score))


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9$]+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOP}


def same_story(a: set[str], b: set[str]) -> bool:
    """Two headlines tell the same story if they share most of their key words."""
    if not a or not b:
        return False
    common = len(a & b)
    return common >= 3 and common / min(len(a), len(b)) >= 0.5


# ---- economic calendar ---------------------------------------------------------------------------
COUNTRY_FA = {"USD": "آمریکا", "EUR": "اروپا", "GBP": "انگلیس", "JPY": "ژاپن", "CNY": "چین", "CAD": "کانادا",
              "AUD": "استرالیا", "NZD": "نیوزیلند", "CHF": "سوئیس", "All": "جهانی"}
EVENT_FA = [
    (r"FOMC|Federal Funds Rate", "تصمیم نرخ بهرهٔ فدرال‌رزرو"), (r"Core CPI", "تورم هسته (CPI)"), (r"\bCPI\b", "تورم مصرف‌کننده (CPI)"),
    (r"Core PCE", "تورم هستهٔ PCE"), (r"\bPCE\b", "تورم PCE"), (r"\bPPI\b", "تورم تولیدکننده (PPI)"),
    (r"Non-Farm", "اشتغال غیرکشاورزی"), (r"Unemployment Rate", "نرخ بیکاری"), (r"Unemployment Claims", "درخواست بیمهٔ بیکاری"),
    (r"\bGDP\b", "تولید ناخالص داخلی"), (r"Retail Sales", "خرده‌فروشی"), (r"ISM", "شاخص ISM"), (r"Powell", "سخنرانی پاول"),
    (r"Main Refinancing Rate|Monetary Policy", "تصمیم نرخ بهره"), (r"Speaks", "سخنرانی"), (r"JOLTS", "فرصت‌های شغلی (JOLTS)"),
]


def event_fa(title: str) -> str | None:
    for rx, fa in EVENT_FA:
        if re.search(rx, title):
            return fa
    return None
