"""Quick live check from the server: Binance REST, Binance kline WebSocket, LBank futures markets.

Usage:  python scripts/check_connectivity.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.data.binance import BinanceFutures  # noqa: E402
from app.data.stream import combined_url  # noqa: E402

OK, FAIL = "✅", "❌"


async def check_binance_rest(cfg) -> bool:
    b = BinanceFutures(cfg.binance_rest)
    try:
        info = await b.exchange_info()
        now = int(time.time() * 1000)
        kl = await b.klines("BTCUSDT", "15m", now - 3_600_000, now)
        print(f"{OK} Binance REST: {len(info['symbols'])} قرارداد، آخرین قیمت BTC = {kl[-1].close}")
        return True
    except Exception as exc:
        print(f"{FAIL} Binance REST: {exc}")
        return False
    finally:
        await b.close()


async def check_binance_ws(cfg) -> bool:
    from websockets.asyncio.client import connect

    url = combined_url(cfg.binance_ws, ["btcusdt@kline_1m"])
    try:
        async with connect(url, open_timeout=10) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            k = json.loads(raw)["data"]["k"]
            print(f"{OK} Binance WebSocket ({cfg.binance_ws}): کندل زنده BTC، قیمت {k['c']}")
            return True
    except Exception as exc:
        print(f"{FAIL} Binance WebSocket ({url}): {exc}")
        return False


async def check_lbank() -> bool:
    try:
        from app.data.lbank import LBankFutures

        lb = LBankFutures()
        try:
            markets = await lb.swap_markets()
            usdt = [k for k, m in markets.items() if m.get("quote") == "USDT"]
            sample = next((s for s in usdt if s.startswith("BTC/")), usdt[0] if usdt else None)
            price = (await lb.ticker(sample))["last"] if sample else None
            print(f"{OK} LBank futures: {len(usdt)} قرارداد USDT، {sample} = {price}")
            return True
        finally:
            await lb.close()
    except Exception as exc:
        print(f"{FAIL} LBank futures: {exc}")
        return False


async def main() -> int:
    cfg = load_settings()
    results = [await check_binance_rest(cfg), await check_binance_ws(cfg), await check_lbank()]
    print("\nنتیجه:", "همه اتصال‌ها سالم است" if all(results) else "بعضی اتصال‌ها مشکل دارند")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
