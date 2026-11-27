import numpy as np
import pandas as pd

from app.lab.styles import build_ctx, combine, pnl, STYLES
from app.portfolio.book import Book
from app.portfolio.targets import COMBOS, combo_weights, member_weights
from tests.test_lab import market


def test_live_weights_reproduce_lab_combination():
    d, f = market(7, n=60, days=700)
    c = build_ctx(d, f)
    members = member_weights(c, cost=0.0)
    for combo in COMBOS:
        w = combo_weights(members, combo)
        live = pnl(c, w, 0.0)["ret"]
        full = {k: pnl(c, STYLES[k][1](c), 0.0, STYLES[k][2]) for k in COMBOS[combo]}
        lab = combine(full, COMBOS[combo])["ret"]
        both = pd.concat([live, lab], axis=1, join="inner").dropna().iloc[40:]
        assert len(both) > 300
        assert np.allclose(both.iloc[:, 0], both.iloc[:, 1], atol=1e-10), combo


def test_book_accounting():
    b = Book(1000.0)
    r = b.rebalance({"AUSDT": 0.5, "BUSDT": -0.5}, fee=0.001)
    assert abs(r["fees"] - 1.0) < 1e-9 and abs(b.equity - 999.0) < 1e-9
    price, fund = b.mark({"AUSDT": 0.10, "BUSDT": 0.10}, {"AUSDT": 0.001, "BUSDT": 0.001})
    assert abs(price) < 1e-9                       # long and short cancel
    assert abs(fund - 0.0) < 1e-9                  # long pays what short receives
    assert abs(b.positions["AUSDT"] - 500 * 1.1) < 1e-9


def test_exec_book_drops_unavailable_and_tiny():
    b = Book(1000.0)
    r = b.rebalance({"AUSDT": 0.5, "BUSDT": 0.003, "CUSDT": -0.3}, allowed={"AUSDT", "BUSDT"}, min_notional=5.0)
    assert set(b.positions) == {"AUSDT"} and r["dropped"] == 2


def test_service_run_creates_and_continues_books():
    import asyncio
    import json as _json
    import sys
    import types

    sys.modules.setdefault("httpx", types.ModuleType("httpx"))  # network client is not used here
    from app.portfolio import service as S

    d, f = market(9, n=60, days=700)
    D = 86_400_000
    last = max(int(x.index[-1]) for x in d.values())
    saved, outbox = {}, []

    class Svc(S.PortfolioService):
        def __init__(self):
            self.state = {}
        async def trading_symbols(self):
            return list(d)
        async def refresh(self, symbols, now_ms):
            pass
        async def load(self, symbols, now_ms):
            cut = S.last_closed_day(now_ms)
            return {k: v[v.index <= cut] for k, v in d.items()}, f
        async def lbank_markets(self, prices):
            return set(list(prices)[:40]), {k: 5.0 for k in prices}
        async def books(self):
            return {k: {"equity": v["equity"], "positions": _json.dumps(v["positions"]), "started_day": v["start"],
                        "last_day": v["last"]} for k, v in self.state.items()}
        async def save(self, combo, mode, start, book, records):
            if records:
                self.state[(combo, mode)] = {"equity": book.equity, "positions": book.positions, "start": start, "last": records[-1][0]}
                saved.setdefault((combo, mode), []).extend(r[0] for r in records)
        async def notify(self, day, report, existing):
            outbox.append(day)

    svc = Svc()
    now = last - 5 * D + D + 3600_000          # 5 days before the end of the data
    asyncio.run(svc.run(now))
    assert len(svc.state) == 4 and all(abs(v["equity"] - S.CAPITAL) < S.CAPITAL * 0.01 for v in svc.state.values())
    asyncio.run(svc.run(now + 3 * D))          # missed two days: catches up day by day
    for k, days in saved.items():
        assert days == sorted(set(days)) and len(days) == 4, (k, days)
    ex = svc.state[("combo_top", "exec")]["positions"]
    assert set(ex) <= set(list(d)[:40])        # executable book holds only coins listed on "LBank"
