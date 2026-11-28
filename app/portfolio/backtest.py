"""Backtest of the live portfolio engine itself: the same books, day by day, over all history.

  .venv/bin/python -m app.portfolio.backtest

Differences from the lab: positions are dollars in a $3000 book, the executable book
keeps only coins listed on LBank TODAY and drops orders below LBank's minimum
(today's list is used for the whole past, which flatters the executable book a little).
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone

import pandas as pd

from app.config import load_settings
from app.data.binance import BinanceFutures
from app.data.store import Store
from app.lab.styles import DISCOVERY_END, build_ctx
from app.portfolio.book import Book
from app.portfolio.service import CAPITAL, PortfolioService
from app.portfolio.targets import COMBOS, combo_weights, member_weights
from app.trend.backtest import load_daily, load_daily_funding

DAY = 86_400_000


STOP_LEVELS = (None, 0.25, 0.40, 0.60)


def simulate(c, weights: pd.DataFrame, allowed: set | None, minimum: dict | float,
             stop: float | None = None, ohl: dict | None = None) -> pd.DataFrame:
    """ohl: {"open"/"high"/"low": DataFrame like c.close}; needed when stop is set."""
    active = weights.abs().sum(axis=1) > 0
    if not active.any():
        return pd.DataFrame()
    days = weights.index[weights.index >= active[active].index[0]]
    book, rows = Book(CAPITAL), []
    prev = None
    for i, d in enumerate(days):
        price = fund = 0.0
        n_stops = 0
        if i:
            stopped = None
            if stop is not None and book.positions:
                held = list(book.positions)
                bars = {s: (ohl["open"].at[d, s], ohl["high"].at[d, s], ohl["low"].at[d, s]) for s in held if s in ohl["open"].columns}
                pc = {s: c.close.at[prev, s] for s in held}
                stopped = book.check_stops(stop, int(d), bars, pc)
                n_stops = len(stopped)
            price, fund = book.mark(c.rets.loc[d].dropna().to_dict(), c.fund.loc[d].dropna().to_dict(), stopped)
        w = weights.loc[d]
        rb = book.rebalance(w[w != 0].to_dict(), allowed, minimum, day=int(d))
        book.update_meta(int(d), c.close.loc[d].dropna().to_dict())
        sm = book.summary()
        rows.append({"day": int(d), "equity": book.equity, "price": price, "funding": fund, "fees": rb["fees"],
                     "dropped": rb["dropped"], "n": sm["n_long"] + sm["n_short"], "gross": sm["gross"], "stops": n_stops})
        prev = d
        if book.equity <= 0:
            break
    return pd.DataFrame(rows).set_index("day")


def stats(eq: pd.Series) -> dict:
    r = eq.pct_change().dropna()
    if len(r) < 30 or r.std() == 0:
        return {}
    years = len(r) / 365
    idx = pd.to_datetime(r.index, unit="ms", utc=True)
    yearly = {str(y): round(float(100 * ((1 + g).prod() - 1)), 1) for y, g in pd.Series(r.to_numpy(), index=idx).groupby(idx.year)}
    return {"sharpe": round(float(r.mean() / r.std() * math.sqrt(365)), 2),
            "cagr": round(float(100 * ((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1)), 1),
            "max_dd": round(float(100 * (eq / eq.cummax() - 1).min()), 1),
            "total": round(float(100 * (eq.iloc[-1] / eq.iloc[0] - 1)), 1), "yearly": yearly}


async def main() -> None:
    cfg = load_settings()
    store = Store(cfg.database_url)
    await store.connect(attempts=1)
    await store.apply_schema()
    binance = BinanceFutures(cfg.binance_rest)
    t0 = time.time()
    try:
        async with store.pool.acquire() as con:
            symbols = [r["symbol"] for r in await con.fetch(
                "SELECT DISTINCT symbol FROM candles WHERE tf='1d' AND open_time < now() - interval '1 day'")]
        daily, funding, ohl_cols = {}, {}, {}
        end = int(time.time() * 1000)
        for s in symbols:
            df = await load_daily(store, s)
            if len(df) >= 60:
                daily[s] = df[["close", "quote_volume"]]
                for k in ("open", "high", "low"):
                    ohl_cols.setdefault(k, {})[s] = df[k]
                funding[s] = await load_daily_funding(store, binance, s, int(df.index[0]), end, False)
        print(f"loaded {len(daily)} coins in {time.time() - t0:.0f}s", flush=True)
        c = build_ctx(daily, funding)
        del daily
        ohl = {k: pd.DataFrame(v).reindex(index=c.close.index, columns=c.close.columns) for k, v in ohl_cols.items()}
        del ohl_cols
        members = member_weights(c)
        prices = {s: float(c.close[s].dropna().iloc[-1]) for s in c.close.columns if c.close[s].notna().any()}
        allowed, minimum = await PortfolioService(store, binance).lbank_markets(prices)
        print(f"{len(allowed)} of {len(prices)} Binance coins are listed on LBank today", flush=True)
        cut = int(datetime.strptime(DISCOVERY_END, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + DAY

        out, curves = {}, {}
        print(f"\n=== portfolio engine backtest — ${CAPITAL:,.0f} per book, fees 0.1%, real funding ===")
        print("exec books are repeated with an emergency stop (close when price moves 25/40/60% against the entry,"
              " checked with the day's high/low; no re-entry in that direction for 7 days)")
        print(f"{'book':<30}{'period':<10}{'Sharpe':>7}{'CAGR%':>7}{'maxDD%':>8}{'total%':>9}  per year")
        variants = [(combo, mode, None) for combo in COMBOS for mode in ("exec", "ideal")]
        variants += [(combo, "exec", lv) for combo in COMBOS for lv in STOP_LEVELS if lv]
        for combo, mode, lv in variants:
            w = combo_weights(members, combo)
            sim = simulate(c, w, allowed if mode == "exec" else None, minimum if mode == "exec" else 0.0, lv, ohl)
            eq = sim["equity"]
            res = {"all": stats(eq), "discovery": stats(eq[eq.index < cut]), "holdout": stats(eq[eq.index >= cut]),
                   "stops": int(sim["stops"].sum()),
                   "avg_positions": round(float(sim["n"].mean()), 1), "avg_dropped": round(float(sim["dropped"].mean()), 1),
                   "fees_total": round(float(sim["fees"].sum()), 2), "funding_total": round(float(sim["funding"].sum()), 2)}
            name = f"{combo}:{mode}" + (f":stop{int(lv * 100)}" if lv else "")
            out[name] = res
            if not lv:
                curves[name] = [[int(t), round(float(v), 2)] for t, v in eq.iloc[::2].items()]
            for period in ("discovery", "holdout"):
                s = res[period]
                g = lambda k: str(s.get(k, "-"))  # noqa: E731
                print(f"{name:<30}{period:<10}{g('sharpe'):>7}{g('cagr'):>7}{g('max_dd'):>8}"
                      f"{g('total'):>9}  {s.get('yearly')}")
            print(f"{'':<30}positions ~{res['avg_positions']}/day, dropped ~{res['avg_dropped']}/day, "
                  f"fees ${res['fees_total']:,.0f}, funding paid ${res['funding_total']:,.0f}, emergency stops {res['stops']}")
        async with store.pool.acquire() as con:
            rid = await con.fetchval(
                "INSERT INTO backtest_runs (params, summary, curve, signals, trades) VALUES ($1::jsonb,$2::jsonb,$3::jsonb,'[]','[]') RETURNING id",
                json.dumps({"kind": "portfolio", "capital": CAPITAL}),
                json.dumps({"kind": "portfolio", "books": out, "discovery_end": DISCOVERY_END,
                            "lbank_listed": len(allowed), "coins": len(prices)}), json.dumps(curves))
        print(f"\nsaved as backtest #{rid}  ({time.time() - t0:.0f}s). Shown in the panel's Portfolio tab.")
    finally:
        await binance.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
