"""Market universe: top-N Binance USDT perpetuals that are also tradable on LBank futures,
plus the smaller analysis set that gets full multi-timeframe analysis."""

from __future__ import annotations

import math
import re

from app.core.models import UniverseEntry

STABLE_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "USD1", "PYUSD", "RLUSD", "USDD", "EUR", "EURI",
}
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")
# Order matters: longest prefix first.
_MULTIPLIER_PREFIXES = (("1000000", 1_000_000), ("1M", 1_000_000), ("10000", 10_000), ("1000", 1_000), ("100", 100))


def normalize_base(base: str) -> tuple[str, int]:
    """1000PEPE -> ('PEPE', 1000); 1MBABYDOGE -> ('BABYDOGE', 1_000_000); BTC -> ('BTC', 1)."""
    b = base.upper()
    for prefix, mult in _MULTIPLIER_PREFIXES:
        rest = b[len(prefix):]
        # only strip if what remains is a real ticker (starts with a letter)
        if b.startswith(prefix) and rest and rest[0].isalpha():
            return rest, mult
    return b, 1


def binance_candidates(exchange_info: dict, tickers: list[dict]) -> list[dict]:
    """Tradable USDT-margined crypto perpetuals joined with 24h stats."""
    stats = {t["symbol"]: t for t in tickers}
    out: list[dict] = []
    for s in exchange_info.get("symbols", []):
        sym = s.get("symbol", "")
        if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT" or not _SYMBOL_RE.match(sym):
            continue
        if s.get("underlyingType", "COIN") != "COIN":  # skip index / TradFi contracts
            continue
        base, mult = normalize_base(s.get("baseAsset", sym[:-4]))
        if base in STABLE_BASES:
            continue
        t = stats.get(sym)
        if not t:
            continue
        high, low = float(t.get("highPrice", 0)), float(t.get("lowPrice", 0))
        out.append(
            {
                "symbol": sym,
                "base": base,
                "multiplier": mult,
                "quote_volume": float(t.get("quoteVolume", 0)),
                "range_pct": (high - low) / low * 100 if low > 0 else 0.0,
            }
        )
    return out


def lbank_index(markets: dict) -> dict[str, str]:
    """Normalized base -> ccxt unified symbol for active LBank USDT perpetuals."""
    index: dict[str, str] = {}
    for unified, m in markets.items():
        if not m.get("swap") or m.get("quote") != "USDT":
            continue
        if m.get("active") is False:
            continue
        base, _ = normalize_base(str(m.get("base", "")))
        index.setdefault(base, unified)
    return index


def build_universe(candidates: list[dict], lbank: dict[str, str], size: int, require_lbank: bool) -> list[UniverseEntry]:
    ranked = sorted(candidates, key=lambda c: c["quote_volume"], reverse=True)
    out: list[UniverseEntry] = []
    for c in ranked:
        lb = lbank.get(c["base"])
        if require_lbank and lb is None:
            continue
        out.append(
            UniverseEntry(
                symbol=c["symbol"],
                base=c["base"],
                multiplier=c["multiplier"],
                quote_volume_24h=c["quote_volume"],
                range_pct_24h=c["range_pct"],
                on_lbank=lb is not None,
                lbank_symbol=lb,
            )
        )
        if len(out) >= size:
            break
    return out


def select_analysis_set(universe: list[UniverseEntry], n: int, always: list[str], volatility_weight: float = 0.0) -> list[str]:
    """Pick n symbols for full analysis by liquidity rank, optionally blended with volatility rank.

    Default is liquidity only: volatile small caps mostly ended up "chaotic" and
    unusable while pushing out larger coins (seen on the live server, 2026-11-27).

    Extreme volatility is capped so a single pump does not dominate the pick.
    Symbols in `always` (e.g. BTC, ETH) are included first when present.
    """
    if n <= 0 or not universe:
        return []
    k = len(universe)

    def pct_rank(values: list[float]) -> list[float]:
        order = sorted(range(k), key=lambda i: values[i])
        ranks = [0.0] * k
        for r, i in enumerate(order):
            ranks[i] = r / (k - 1) if k > 1 else 1.0
        return ranks

    liq = pct_rank([math.log10(max(u.quote_volume_24h, 1.0)) for u in universe])
    vol = pct_rank([min(u.range_pct_24h, 25.0) for u in universe])
    scored = sorted(range(k), key=lambda i: (1 - volatility_weight) * liq[i] + volatility_weight * vol[i], reverse=True)

    present = {u.symbol for u in universe}
    picked = [s for s in always if s in present][:n]
    for i in scored:
        if len(picked) >= n:
            break
        sym = universe[i].symbol
        if sym not in picked:
            picked.append(sym)
    return picked
