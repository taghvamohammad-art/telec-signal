"""Collect positions of Hyperliquid's consistently profitable large traders.

This only gathers data. Whether following them helps is tested months later,
using only what was known at each moment (the wallet list is saved per day).

  .venv/bin/python -m app.whales.collector          # service loop
  .venv/bin/python -m app.whales.collector --once   # one round, prints a summary
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    import httpx
except ImportError:  # pragma: no cover - unit tests run without it
    httpx = None

from app.config import load_settings
from app.data.store import Store

log = logging.getLogger("telec.whales")

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
FLOW_EVERY_S = 15 * 60
POSITIONS_EVERY_S = 60 * 60
MAX_WALLETS = 200


@dataclass(frozen=True)
class Rules:
    min_account: float = 100_000      # USD in the account now
    min_pnl_all: float = 250_000      # proven over time, not one lucky week
    min_roi_all: float = 0.0
    min_pnl_month: float = 0.0        # still working this month
    max_turnover: float = 300.0       # month volume / account; above this it is a market-making bot


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_row(row: dict) -> dict | None:
    addr = row.get("ethAddress") or row.get("user")
    if not addr:
        return None
    perf = {}
    for item in row.get("windowPerformances") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            perf[item[0]] = item[1]
    allt, month = perf.get("allTime", {}), perf.get("month", {})
    return {"address": addr.lower(), "account_value": _num(row.get("accountValue")),
            "pnl_all": _num(allt.get("pnl")), "roi_all": _num(allt.get("roi")),
            "pnl_month": _num(month.get("pnl")), "roi_month": _num(month.get("roi")),
            "vlm_month": _num(month.get("vlm"))}


def qualifies(w: dict, r: Rules = Rules()) -> bool:
    if w["account_value"] < r.min_account or w["pnl_all"] < r.min_pnl_all:
        return False
    if w["roi_all"] <= r.min_roi_all or w["pnl_month"] <= r.min_pnl_month:
        return False
    return w["vlm_month"] / w["account_value"] <= r.max_turnover


def iter_rows(text: str):
    """Yield leaderboard rows one by one (the file is ~40 MB; this keeps memory low)."""
    dec = json.JSONDecoder()
    key = text.find('"leaderboardRows"')
    i = text.find("[", key if key >= 0 else 0) + 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " \r\n\t,":
            i += 1
        if i >= n or text[i] == "]":
            return
        obj, i = dec.raw_decode(text, i)
        yield obj


def select(rows, rules: Rules = Rules(), limit: int = MAX_WALLETS) -> list[dict]:
    chosen = []
    for row in rows:
        w = parse_row(row) if isinstance(row, dict) else None
        if w and qualifies(w, rules):
            chosen.append(w)
    chosen.sort(key=lambda w: w["pnl_all"], reverse=True)
    return chosen[:limit]


def parse_positions(state: dict) -> tuple[float, list[dict]]:
    acct = _num((state.get("marginSummary") or {}).get("accountValue"))
    out = []
    for ap in state.get("assetPositions") or []:
        p = ap.get("position") or {}
        size = _num(p.get("szi"))
        if size == 0 or not p.get("coin"):
            continue
        value = abs(_num(p.get("positionValue")))
        out.append({"coin": p["coin"], "size": size, "notional": value if size > 0 else -value,
                    "entry_px": _num(p.get("entryPx")) or None,
                    "leverage": _num((p.get("leverage") or {}).get("value")) or None,
                    "upnl": _num(p.get("unrealizedPnl"))})
    return acct, out


def aggregate(snapshots: dict[str, list[dict]]) -> dict[str, dict]:
    flow: dict[str, dict] = defaultdict(lambda: {"n_long": 0, "n_short": 0, "long_notional": 0.0, "short_notional": 0.0})
    for positions in snapshots.values():
        for p in positions:
            f = flow[p["coin"]]
            if p["size"] > 0:
                f["n_long"] += 1
                f["long_notional"] += p["notional"]
            else:
                f["n_short"] += 1
                f["short_notional"] += -p["notional"]
    return dict(flow)


class Collector:
    def __init__(self, store: Store):
        self.store = store
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), headers={"User-Agent": "telec-signal"})
        self.stop = asyncio.Event()
        self.wallets: list[str] = []
        self.wallets_day = None
        self.last_positions = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def refresh_wallets(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self.wallets_day == today and self.wallets:
            return
        async with self.store.pool.acquire() as con:
            rows = await con.fetch("SELECT address FROM whale_wallets WHERE day=$1 ORDER BY rank", today)
        if rows:
            self.wallets, self.wallets_day = [r["address"] for r in rows], today
            return
        with tempfile.NamedTemporaryFile("w+b", suffix=".json", delete=True) as f:
            async with self.http.stream("GET", LEADERBOARD_URL, timeout=httpx.Timeout(180, connect=20)) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
            f.flush()
            f.seek(0)
            text = f.read().decode("utf-8")
        chosen = select(iter_rows(text))
        del text
        if not chosen:
            raise RuntimeError("leaderboard parsed but no wallet qualified (format change?)")
        async with self.store.pool.acquire() as con:
            await con.executemany(
                """INSERT INTO whale_wallets (day, address, rank, account_value, pnl_all, roi_all, pnl_month, roi_month, vlm_month)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING""",
                [(today, w["address"], i, w["account_value"], w["pnl_all"], w["roi_all"], w["pnl_month"],
                  w["roi_month"], w["vlm_month"]) for i, w in enumerate(chosen, 1)])
        self.wallets, self.wallets_day = [w["address"] for w in chosen], today
        log.info("chose %d wallets for %s (top all-time PnL %.0f USD)", len(chosen), today, chosen[0]["pnl_all"])

    async def state(self, address: str) -> dict | None:
        for attempt in range(4):
            try:
                r = await self.http.post(INFO_URL, json={"type": "clearinghouseState", "user": address})
                if r.status_code == 429:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError as exc:
                log.debug("state %s: %s", address, exc)
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def round(self) -> dict:
        await self.refresh_wallets()
        ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        snaps: dict[str, list[dict]] = {}
        accounts: dict[str, float] = {}
        for addr in self.wallets:
            if self.stop.is_set():
                break
            st = await self.state(addr)
            if st is not None:
                accounts[addr], snaps[addr] = parse_positions(st)
            await asyncio.sleep(0.25)  # ~240 requests/min, well under the per-IP limit
        if not snaps:
            raise RuntimeError("no wallet state could be read from Hyperliquid")
        flow = aggregate(snaps)
        save_positions = time.time() - self.last_positions >= POSITIONS_EVERY_S - 60
        async with self.store.pool.acquire() as con:
            await con.executemany(
                """INSERT INTO whale_flow (ts, coin, wallets, n_long, n_short, long_notional, short_notional)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
                [(ts, c, len(snaps), f["n_long"], f["n_short"], f["long_notional"], f["short_notional"]) for c, f in flow.items()])
            if save_positions:
                await con.executemany(
                    """INSERT INTO whale_positions (ts, address, coin, size, notional, entry_px, leverage, upnl, account_value)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING""",
                    [(ts, a, p["coin"], p["size"], p["notional"], p["entry_px"], p["leverage"], p["upnl"], accounts[a])
                     for a, ps in snaps.items() for p in ps])
                self.last_positions = time.time()
        return {"wallets": len(snaps), "coins": len(flow), "flow": flow}

    async def loop(self) -> None:
        while not self.stop.is_set():
            started = time.time()
            try:
                res = await self.round()
                log.info("snapshot: %d wallets, %d coins", res["wallets"], res["coins"])
            except Exception:
                log.exception("whale round failed")
            wait = max(30.0, FLOW_EVERY_S - (time.time() - started))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass


def print_summary(res: dict) -> None:
    top = sorted(res["flow"].items(), key=lambda kv: kv[1]["long_notional"] + kv[1]["short_notional"], reverse=True)[:15]
    print(f"\n{res['wallets']} wallets read, positions in {res['coins']} coins. Biggest:")
    print(f"{'coin':<10}{'longs':>7}{'shorts':>7}{'long $M':>10}{'short $M':>10}")
    for c, f in top:
        print(f"{c:<10}{f['n_long']:>7}{f['n_short']:>7}{f['long_notional'] / 1e6:>10.1f}{f['short_notional'] / 1e6:>10.1f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if os.environ.get("WHALES_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
        log.info("WHALES_ENABLED is off; collector idle")
        while True:
            await asyncio.sleep(3600)
    store = Store(cfg.database_url)
    await store.connect()
    await store.apply_schema()
    c = Collector(store)
    try:
        if a.once:
            print_summary(await c.round())
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, c.stop.set)
        await c.loop()
    finally:
        await c.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
