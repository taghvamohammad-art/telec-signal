"""Daily live paper test of the lab combinations (service telec-portfolio).

Once a day after 00:10 UTC:
  1. fetch the newest daily candles and funding of every trading Binance perpetual
  2. compute today's target weights of each combination (app/portfolio/targets.py)
  3. update four paper books: each combination x {ideal, executable on LBank with real minimums}
  4. queue a Telegram summary

  .venv/bin/python -m app.portfolio.service          # service loop
  .venv/bin/python -m app.portfolio.service --once   # run now
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone

import pandas as pd

from app.alerts import AdminAlerter
from app.config import load_settings
from app.data.binance import BinanceFutures
from app.data.candles import clean_batch
from app.data.store import Store, dt_to_ms, ms_to_dt
from app.data.universe import all_perp_symbols, lbank_index, normalize_base
from app.lab.styles import build_ctx
from app.portfolio.book import Book
from app.portfolio.targets import COMBO_FA, COMBOS, combo_weights, contributions, member_weights

log = logging.getLogger("telec.portfolio")
DAY = 86_400_000
HISTORY_DAYS = 560
CAPITAL = 3000.0
RUN_AFTER_S = 10 * 60          # minutes after the UTC day close
BOOKS = [(c, m) for c in COMBOS for m in ("exec", "ideal")]
MODE_FA = {"exec": "اجرایی روی ال‌بانک", "ideal": "ایده‌آل (بایننس، بدون حداقل سفارش)"}


def last_closed_day(now_ms: int) -> int:
    return now_ms // DAY * DAY - DAY


class PortfolioService:
    def __init__(self, store: Store, binance: BinanceFutures, alerter: AdminAlerter | None = None):
        self.store = store
        self.binance = binance
        self.alerter = alerter
        self.stop = asyncio.Event()

    # ---- data ---------------------------------------------------------------
    async def trading_symbols(self) -> list[str]:
        listed = all_perp_symbols(await self.binance.exchange_info())
        return sorted(s for s, st in listed.items() if st == "TRADING")

    async def refresh(self, symbols: list[str], now_ms: int) -> None:
        start = now_ms - HISTORY_DAYS * DAY
        for i, sym in enumerate(symbols, 1):
            try:
                async with self.store.pool.acquire() as con:
                    last = await con.fetchval("SELECT max(open_time) FROM candles WHERE symbol=$1 AND tf='1d'", sym)
                    flast = await con.fetchval("SELECT max(ts) FROM funding_rates WHERE symbol=$1", sym)
                frm = dt_to_ms(last) + DAY if last else start
                if frm < last_closed_day(now_ms) + DAY:
                    ok, _ = clean_batch(await self.binance.klines_range(sym, "1d", frm, now_ms), now_ms)
                    await self.store.upsert_candles(ok)
                ffrm = dt_to_ms(flast) + 1 if flast else start
                if ffrm < now_ms - 8 * 3600_000:
                    hist = await self.binance.funding_history(sym, ffrm, now_ms)
                    await self.store.save_funding([(sym, t, r, m) for t, r, m in hist])
                    await asyncio.sleep(0.4)
            except Exception as exc:
                log.warning("refresh %s: %s", sym, exc)
            if i % 100 == 0:
                log.info("refreshed %d/%d", i, len(symbols))

    async def load(self, symbols: list[str], now_ms: int) -> tuple[dict, dict]:
        since = ms_to_dt(now_ms - HISTORY_DAYS * DAY)
        daily, funding = {}, {}
        async with self.store.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT symbol, open_time, close, quote_volume FROM candles
                   WHERE tf='1d' AND open_time >= $1 AND symbol = ANY($2::text[]) ORDER BY symbol, open_time""", since, symbols)
            frows = await con.fetch(
                "SELECT symbol, ts, rate FROM funding_rates WHERE ts >= $1 AND symbol = ANY($2::text[])", since, symbols)
        cut = last_closed_day(now_ms)
        df = pd.DataFrame([dict(r) for r in rows])
        if df.empty:
            return {}, {}
        df["t"] = [dt_to_ms(t) for t in df.pop("open_time")]
        df = df[df["t"] <= cut]
        for sym, g in df.groupby("symbol"):
            if len(g) >= 30:
                daily[sym] = g.set_index("t")[["close", "quote_volume"]].astype(float)
        fd = pd.DataFrame([dict(r) for r in frows])
        if not fd.empty:
            fd["day"] = [dt_to_ms(t) // DAY * DAY for t in fd.pop("ts")]
            for sym, g in fd.groupby("symbol"):
                funding[sym] = g.groupby("day")["rate"].sum()
        return daily, funding

    async def lbank_markets(self, prices: dict[str, float]) -> tuple[set[str], dict[str, float]]:
        """Binance symbols that exist on LBank, and the smallest order (USDT) for each."""
        from app.data.lbank import LBankFutures

        lb = LBankFutures()
        try:
            markets = await lb.swap_markets()
        finally:
            await lb.close()
        index = lbank_index(markets)
        allowed, minimum = set(), {}
        for sym, price in prices.items():
            base, mult = normalize_base(sym[:-4])
            unified = index.get(base)
            if not unified:
                continue
            m = markets[unified]
            lim = m.get("limits") or {}
            amount_min = ((lim.get("amount") or {}).get("min") or 0) * float(m.get("contractSize") or 1)
            cost_min = (lim.get("cost") or {}).get("min") or 0
            lb_price = price / (mult or 1)
            allowed.add(sym)
            minimum[sym] = max(5.0, float(amount_min) * lb_price, float(cost_min))
        return allowed, minimum

    # ---- books ---------------------------------------------------------------
    async def books(self) -> dict[tuple[str, str], dict]:
        async with self.store.pool.acquire() as con:
            rows = await con.fetch("SELECT * FROM portfolio_books")
        return {(r["combo"], r["mode"]): dict(r) for r in rows}

    async def run(self, now_ms: int | None = None) -> dict:
        now_ms = now_ms or int(time.time() * 1000)
        day = last_closed_day(now_ms)
        t0 = time.time()
        symbols = await self.trading_symbols()
        await self.refresh(symbols, now_ms)
        daily, funding = await self.load(symbols, now_ms)
        if "BTCUSDT" not in daily or int(daily["BTCUSDT"].index[-1]) < day:
            raise RuntimeError("daily candles are not up to date yet")
        c = build_ctx(daily, funding)
        members = member_weights(c)
        weights = {combo: combo_weights(members, combo) for combo in COMBOS}
        prices = {s: float(c.close[s].iloc[-1]) for s in c.close.columns if pd.notna(c.close[s].iloc[-1])}
        try:
            allowed, minimum = await self.lbank_markets(prices)
        except Exception as exc:
            log.warning("LBank markets unavailable (%s); executable books keep yesterday's positions", exc)
            allowed, minimum = None, {}
        existing = await self.books()
        report = {}
        for combo, mode in BOOKS:
            if mode == "exec" and allowed is None:
                continue
            row = existing.get((combo, mode))
            w = weights[combo]
            if row is None:
                book, start = Book(CAPITAL), day
                days = [day]
            else:
                load = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # noqa: E731
                book = Book(float(row["equity"]), {k: float(v) for k, v in load(row["positions"]).items()}, load(row.get("meta")))
                start = int(row["started_day"])
                days = [d for d in w.index if int(row["last_day"]) < d <= day]
                if any(s not in book.meta for s in book.positions):
                    await self.rebuild_meta(row, book, c.close)
            records = []
            for d in days:
                rets = c.rets.loc[d].dropna().to_dict() if d in c.rets.index else {}
                fund = c.fund.loc[d].dropna().to_dict() if d in c.fund.index else {}
                price_pnl, fund_paid = (0.0, 0.0) if row is None and d == days[0] else book.mark(rets, fund)
                target = w.loc[d][w.loc[d] != 0].to_dict()
                rb = book.rebalance(target, allowed if mode == "exec" else None, minimum if mode == "exec" else 0.0)
                px = c.close.loc[d].dropna().to_dict() if d in c.close.index else {}
                book.update_meta(d, px)
                src = contributions(members, combo, d, list(book.positions))
                records.append((d, book.equity, price_pnl, fund_paid, rb, book.summary(), book.snapshot(px, src)))
            await self.save(combo, mode, start, book, records)
            report[(combo, mode)] = {"equity": book.equity, "start": start, "records": records}
        log.info("portfolio run for %s done in %.0fs", datetime.fromtimestamp(day / 1000, tz=timezone.utc).date(), time.time() - t0)
        await self.notify(day, report, existing)
        return report

    async def rebuild_meta(self, row: dict, book: Book, close: pd.DataFrame) -> None:
        """Books created before entry prices were tracked: find each position's opening day from history."""
        async with self.store.pool.acquire() as con:
            hist = await con.fetch("SELECT day, positions FROM portfolio_days WHERE book_id=$1 ORDER BY day DESC", row["id"])
        days = []
        for h in hist:
            pos = json.loads(h["positions"]) if isinstance(h["positions"], str) else (h["positions"] or {})
            days.append((dt_to_ms(h["day"]), {k: (v["n"] if isinstance(v, dict) else v) for k, v in pos.items()}))
        for s, n in book.positions.items():
            if s in book.meta:
                continue
            side, since = (1 if n > 0 else -1), None
            for d, pos in days:
                v = pos.get(s)
                if v is None or (v > 0) != (side > 0):
                    break
                since = d
            since = since or int(row["last_day"])
            px = close[s].get(since) if s in close else None
            book.meta[s] = {"entry": float(px) if px is not None and px == px else None, "since": since, "side": side}

    async def save(self, combo: str, mode: str, start: int, book: Book, records: list) -> None:
        if not records:
            return
        async with self.store.pool.acquire() as con:
            async with con.transaction():
                bid = await con.fetchval(
                    """INSERT INTO portfolio_books (combo, mode, capital, started_day, last_day, equity, positions, meta)
                       VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)
                       ON CONFLICT (combo, mode) DO UPDATE SET last_day=$5, equity=$6, positions=$7::jsonb, meta=$8::jsonb, updated_at=now()
                       RETURNING id""",
                    combo, mode, CAPITAL, start, records[-1][0], book.equity, json.dumps(book.positions), json.dumps(book.meta))
                for d, eq, price, fund, rb, sm, pos in records:
                    await con.execute(
                        """INSERT INTO portfolio_days (book_id, day, equity, price_pnl, funding, fees, turnover, dropped,
                                                       n_long, n_short, gross, net, positions)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
                           ON CONFLICT (book_id, day) DO NOTHING""",
                        bid, ms_to_dt(d), eq, price, fund, rb["fees"], rb["turnover"], rb["dropped"],
                        sm["n_long"], sm["n_short"], sm["gross"], sm["net"], json.dumps(pos))

    async def notify(self, day: int, report: dict, existing: dict) -> None:
        if not any(r["records"] for r in report.values()):
            return
        date = datetime.fromtimestamp(day / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        lines = [f"<b>پرتفوی آزمایشی — {date}</b>"]
        for (combo, mode), r in report.items():
            if mode != "exec" or not r["records"]:
                continue
            eq = r["equity"]
            prev = r["records"][-2][1] if len(r["records"]) > 1 else float(existing.get((combo, mode), {}).get("equity") or 0) or None
            day_pct = (eq / prev - 1) * 100 if prev else 0.0
            total = (eq / CAPITAL - 1) * 100
            _, _, _, _, rb, sm, pos = r["records"][-1]
            before = rb["before"]
            opened = [s for s in pos if s not in before]
            closed = [s for s in before if s not in pos]
            flipped = [s for s in pos if s in before and (pos[s]["n"] > 0) != (before[s] > 0)]
            lines.append(f"\n<b>{COMBO_FA[combo]}</b>\nموجودی: {eq:,.0f} دلار ({total:+.2f}% از شروع، امروز {day_pct:+.2f}%)\n"
                         f"پوزیشن‌ها: {sm['n_long']} خرید، {sm['n_short']} فروش؛ اهرم کل {sm['gross']:.2f}x")
            if opened or closed or flipped:
                show = lambda xs: "، ".join(x.replace("USDT", "") for x in xs[:8]) + (" …" if len(xs) > 8 else "")  # noqa: E731
                if opened:
                    lines.append(f"باز شد: {show(opened)}")
                if closed:
                    lines.append(f"بسته شد: {show(closed)}")
                if flipped:
                    lines.append(f"تغییر جهت: {show(flipped)}")
        lines.append("\nجزئیات در تب «پرتفوی» سایت. این آزمایش با پول مجازی است.")
        async with self.store.pool.acquire() as con:
            await con.execute("INSERT INTO bot_outbox (text) VALUES ($1)", "\n".join(lines))

    async def loop(self) -> None:
        while not self.stop.is_set():
            now = int(time.time() * 1000)
            due = last_closed_day(now)
            books = await self.books()
            behind = len(books) < len(BOOKS) or any(int(b["last_day"]) < due for b in books.values())
            if behind and now - (due + DAY) >= RUN_AFTER_S * 1000:
                try:
                    await self.run(now)
                except Exception as exc:
                    log.exception("portfolio run failed")
                    if self.alerter:
                        await self.alerter.send("portfolio", f"خطا در پرتفوی آزمایشی: {exc}")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=600)
            except asyncio.TimeoutError:
                pass


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    store = Store(cfg.database_url)
    await store.connect()
    await store.apply_schema()
    binance = BinanceFutures(cfg.binance_rest)
    svc = PortfolioService(store, binance, AdminAlerter(cfg.admin_bot_token, cfg.admin_chat_id))
    try:
        if a.once:
            rep = await svc.run()
            for (combo, mode), r in rep.items():
                print(f"{combo:<14} {mode:<5} equity {r['equity']:,.2f}  days processed {len(r['records'])}")
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, svc.stop.set)
        await svc.loop()
    finally:
        await binance.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
