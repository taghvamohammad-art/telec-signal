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
    saved, outbox, snaps = {}, [], []

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
            return {k: {"id": 1, "equity": v["equity"], "positions": _json.dumps(v["positions"]), "meta": _json.dumps(v["meta"]),
                        "started_day": v["start"], "last_day": v["last"]} for k, v in self.state.items()}
        async def save(self, combo, mode, start, book, records):
            if records:
                self.state[(combo, mode)] = {"equity": book.equity, "positions": book.positions, "meta": book.meta,
                                             "start": start, "last": records[-1][0]}
                snaps.append(records[-1][-1])
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
    snap = snaps[-1]
    assert snap and all(v["entry"] and v["since"] <= S.last_closed_day(now + 3 * D) and v["px"] for v in snap.values())
    meta = svc.state[("combo_top", "exec")]["meta"]
    assert set(meta) == set(svc.state[("combo_top", "exec")]["positions"])
    assert any(m["since"] < S.last_closed_day(now + 3 * D) for m in meta.values())   # some held since an earlier day
    ex = svc.state[("combo_top", "exec")]["positions"]
    assert set(ex) <= set(list(d)[:40])        # executable book holds only coins listed on "LBank"


def test_engine_backtest_simulation_matches_ideal_returns():
    import sys
    import types

    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    from app.portfolio.backtest import simulate

    d, f = market(11, n=60, days=600)
    c = build_ctx(d, f)
    w = combo_weights(member_weights(c), "combo_top")
    sim = simulate(c, w, None, 0.0)
    book_ret = sim["equity"].pct_change().dropna()
    lab = pnl(c, w, 0.001)["ret"].reindex(book_ret.index)
    # dollar book and weight-based returns differ only by compounding inside the day: tiny
    assert (book_ret - lab).abs().max() < 2e-3
    ex = simulate(c, w, set(list(d)[:30]), 5.0)
    assert ex["dropped"].sum() > 0


def test_rebuild_meta_finds_opening_day():
    import asyncio
    import sys
    import types
    from datetime import datetime, timezone

    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    from app.portfolio.service import PortfolioService

    D = 86_400_000
    dt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)  # noqa: E731
    hist = [{"day": dt(3 * D), "positions": {"AUSDT": 10.0, "BUSDT": -5.0}},          # newest first, old number format
            {"day": dt(2 * D), "positions": {"AUSDT": 9.0, "BUSDT": 4.0}},
            {"day": dt(1 * D), "positions": {"AUSDT": 8.0}}]

    class Con:
        async def fetch(self, *a):
            return hist

    class Acq:
        async def __aenter__(self):
            return Con()
        async def __aexit__(self, *a):
            return False

    store = types.SimpleNamespace(pool=types.SimpleNamespace(acquire=lambda: Acq()))
    svc = PortfolioService(store, None)
    book = Book(100.0, {"AUSDT": 10.0, "BUSDT": -5.0})
    close = pd.DataFrame({"AUSDT": [1.0, 2.0, 3.0], "BUSDT": [7.0, 8.0, 9.0]}, index=[D, 2 * D, 3 * D])
    asyncio.run(svc.rebuild_meta({"id": 1, "last_day": 3 * D}, book, close))
    assert book.meta["AUSDT"] == {"entry": 1.0, "since": D, "side": 1}       # long since day 1
    assert book.meta["BUSDT"] == {"entry": 9.0, "since": 3 * D, "side": -1}  # flipped to short on day 3


def test_contributions_add_up_to_the_position():
    from app.portfolio.targets import contributions

    d, f = market(12, n=60, days=600)
    c = build_ctx(d, f)
    members = member_weights(c)
    w = combo_weights(members, "combo_all_pos")
    day = w.index[-1]
    held = [s for s, v in w.loc[day].items() if v != 0]
    src = contributions(members, "combo_all_pos", day, held)
    assert set(src) == set(held)
    for s in held:
        assert 94 <= sum(x[3] for x in src[s]) <= 106  # each share is rounded and all(x[1] for x in src[s])
        # the biggest contributor usually points the same way as the position
    agree = sum(src[s][0][2] == (1 if w.loc[day, s] > 0 else -1) for s in held)
    assert agree >= 0.7 * len(held)


def test_emergency_stop_closes_blocks_and_charges():
    D = 86_400_000
    b = Book(1000.0)
    b.rebalance({"AUSDT": 0.1, "BUSDT": -0.1}, fee=0.0, day=D)
    b.update_meta(D, {"AUSDT": 100.0, "BUSDT": 10.0})
    # day 2: A dips to 55 (long stop at 60 hit), B spikes to 15 (short stop at 14 hit, gapped open at 14.5)
    st = b.check_stops(0.4, 2 * D, {"AUSDT": (95.0, 101.0, 55.0), "BUSDT": (14.5, 15.0, 14.0)}, {"AUSDT": 100.0, "BUSDT": 10.0})
    assert abs(st["AUSDT"] - (-0.4)) < 1e-12 and abs(st["BUSDT"] - 0.45) < 1e-12
    eq0 = b.equity
    b.mark({"AUSDT": -0.05, "BUSDT": 0.2}, {}, st)
    assert not b.positions
    assert abs(b.equity - (eq0 - 40 - 45 - 0.001 * (60 + 145))) < 1e-9
    b.rebalance({"AUSDT": 0.1, "BUSDT": 0.1}, fee=0.0, day=3 * D)        # A long blocked; B long is the other way: allowed
    assert set(b.positions) == {"BUSDT"}
    b.rebalance({"AUSDT": 0.1, "BUSDT": 0.1}, fee=0.0, day=10 * D)       # cooldown over
    assert set(b.positions) == {"AUSDT", "BUSDT"}


def test_simulate_with_stops():
    import sys
    import types

    sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    from app.portfolio.backtest import simulate

    d, f = market(13, n=50, days=500)
    c = build_ctx(d, f)
    ohl = {"open": c.close.shift(1), "high": c.close * 1.15, "low": c.close * 0.85}
    w = combo_weights(member_weights(c), "combo_all_pos")
    none = simulate(c, w, None, 0.0)
    far = simulate(c, w, None, 0.0, 5.0, ohl)       # never reachable for longs: same as no stop for most days
    tight = simulate(c, w, None, 0.0, 0.05, ohl)
    assert none["stops"].sum() == 0 and tight["stops"].sum() > 50
    assert far["stops"].sum() < tight["stops"].sum()


def test_panel_fills_entries_for_days_saved_before_tracking():
    import asyncio
    from datetime import datetime, timezone

    from app.web.repo import _fill_entries

    d = lambda k: datetime(2026, 11, 24 + k, tzinfo=timezone.utc)  # noqa: E731
    hist = [{"day": d(3), "positions": {"AUSDT": 10.0, "BUSDT": -4.0}},
            {"day": d(2), "positions": {"AUSDT": 9.0, "BUSDT": 3.0}},
            {"day": d(1), "positions": {"AUSDT": 8.0}}]
    candles = [{"symbol": "AUSDT", "open_time": d(1), "close": 100.0}, {"symbol": "AUSDT", "open_time": d(3), "close": 110.0},
               {"symbol": "BUSDT", "open_time": d(3), "close": 7.0}]

    class Con:
        async def fetch(self, sql, *a):
            return hist if "portfolio_days" in sql else candles

    out = asyncio.run(_fill_entries(Con(), 1, d(3), {"AUSDT": 10.0, "BUSDT": -4.0}))
    assert out["AUSDT"]["entry"] == 100.0 and out["AUSDT"]["px"] == 110.0 and out["AUSDT"]["n"] == 10.0
    assert out["BUSDT"]["entry"] == 7.0 and out["BUSDT"]["px"] == 7.0      # flipped to short on the last day
