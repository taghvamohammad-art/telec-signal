"""Portfolio simulation of backtest candidates with the paper-trading rules.

Two views:
  * per-signal result in R (every signal, no portfolio limits): measures the rules
  * portfolio equity (sizing, max open, same-side cap, fees, funding): what an
    account following the signals would have experienced
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from app.backtest.replay import funding_at
from app.core.timeframes import tf_ms
from app.paper.sim import FUNDING_HOURS, Trade, size_position, stats

M15 = tf_ms("15m")
HOUR = 3_600_000


def _bars(df: pd.DataFrame) -> dict[int, tuple[float, float, float, float]]:
    return {int(t): (o, h, l, c) for t, o, h, l, c in zip(df.index, df["open"], df["high"], df["low"], df["close"])}


def _is_funding_time(t: int) -> bool:
    return t % HOUR == 0 and (t // HOUR) % 24 in FUNDING_HOURS


def signal_results(cands: list[dict], candles: dict[str, pd.DataFrame], fee_rate: float) -> list[dict]:
    """Each signal on its own, 1 unit of risk: result in R after fees (no funding)."""
    out = []
    for c in cands:
        bars = candles[c["symbol"]]
        idx = bars.index.to_numpy(dtype=np.int64)
        fill = c["bar_time"] + M15
        k = int(np.searchsorted(idx, fill))
        if k >= len(idx) or idx[k] != fill:
            continue
        entry = float(bars["open"].iloc[k])
        side = c["side"]
        if side * (entry - c["stop"]) <= 0 or side * (c["tp1"] - entry) <= 0:
            out.append({**c, "status": "skipped", "r": 0.0, "exit_reason": "gap"})
            continue
        risk = abs(entry - c["stop"])
        t = Trade(side, entry, 1.0 / risk, c["stop"], c["tp1"], c["tp2"], fee_rate)
        t.open_fee(fill)
        hi, lo = bars["high"].to_numpy(), bars["low"].to_numpy()
        closed_at = None
        for j in range(k, len(idx)):
            t.on_candle(int(idx[j]), hi[j], lo[j])
            if t.closed:
                closed_at = int(idx[j]) + M15
                break
        out.append({**c, "status": "closed" if t.closed else "open", "fill": entry, "r": round(t.realized, 3),
                    "exit_reason": t.exit_reason, "closed_at": closed_at})
    return out


def simulate_portfolio(
    cands: list[dict],
    candles: dict[str, pd.DataFrame],
    funding: dict[str, pd.Series],
    capital: float = 1000.0,
    fee_rate: float = 0.0006,
    max_open: int = 5,
    max_same_side: int = 3,
) -> dict:
    bars = {s: _bars(df) for s, df in candles.items()}
    fills: dict[int, list[dict]] = defaultdict(list)
    for c in cands:
        fills[c["bar_time"] + M15].append(c)
    if not fills:
        return {"trades": [], "curve": [], "stats": stats([], [capital]), "final_equity": capital}
    times = sorted({t for b in bars.values() for t in b if t >= min(fills)})

    realized_total = 0.0
    open_trades: list[tuple[dict, Trade]] = []
    done: list[dict] = []
    curve = [{"t": min(fills), "equity": capital}]

    for t in times:
        # 1. fills at this bar's open
        for c in sorted(fills.get(t, []), key=lambda c: -c["score"]):
            bar = bars[c["symbol"]].get(t)
            rec = {**c, "fill_time": t}
            reason = None
            if bar is None:
                reason = "no bar"
            elif any(oc["symbol"] == c["symbol"] for oc, _ in open_trades):
                reason = "same symbol open"
            elif len(open_trades) >= max_open:
                reason = "max open"
            elif sum(1 for oc, _ in open_trades if oc["side"] == c["side"]) >= max_same_side:
                reason = "max same side"
            elif c["side"] * (bar[0] - c["stop"]) <= 0 or c["side"] * (c["tp1"] - bar[0]) <= 0:
                reason = "gap past levels"
            sizing = None
            if reason is None:
                equity = capital + realized_total
                used = sum(tr.qty * tr.entry / oc["leverage"] for oc, tr in open_trades)
                sizing = size_position(equity, equity - used, max_open - len(open_trades), c["score"], bar[0], c["stop"], fee_rate)
                if sizing is None:
                    reason = "no margin"
            if reason:
                done.append({**rec, "status": "skipped", "skip_reason": reason, "pnl": 0.0})
                continue
            tr = Trade(c["side"], bar[0], sizing.qty, c["stop"], c["tp1"], c["tp2"], fee_rate)
            tr.open_fee(t)
            rec.update(entry=bar[0], leverage=sizing.leverage, risk_pct=sizing.risk_pct)
            open_trades.append((rec, tr))

        # 2. funding settled at this bar's close, then 3. the bar itself
        still = []
        for rec, tr in open_trades:
            bar = bars[rec["symbol"]].get(t)
            if bar is None:
                still.append((rec, tr))
                continue
            if _is_funding_time(t + M15):
                rate = funding_at(funding.get(rec["symbol"]), t + M15)
                if rate:
                    tr.apply_funding(t + M15, rate, bar[3])
            tr.on_candle(t, bar[1], bar[2])
            if tr.closed:
                realized_total += tr.realized
                done.append({**rec, "status": "closed", "pnl": round(tr.realized, 4), "fees": tr.fees,
                             "funding": tr.funding, "exit_reason": tr.exit_reason, "closed_at": t + M15})
                curve.append({"t": t + M15, "equity": round(capital + realized_total, 2)})
            else:
                still.append((rec, tr))
        open_trades = still

    # mark still-open trades at the last close (not counted in closed stats)
    for rec, tr in open_trades:
        last = bars[rec["symbol"]][max(bars[rec["symbol"]])]
        done.append({**rec, "status": "open", "pnl": round(tr.realized + tr.unrealized(last[3]), 4), "exit_reason": None})

    closed = [d for d in done if d["status"] == "closed"]
    return {
        "trades": done,
        "curve": curve,
        "stats": stats([d["pnl"] for d in closed], [p["equity"] for p in curve]),
        "final_equity": round(capital + realized_total, 2),
    }


def breakdown(results: list[dict], key) -> dict:
    """Group per-signal R results: count, win rate, average R, total R."""
    groups: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.get("status") != "closed":
            continue
        for k in (key(r) if callable(key) else [r.get(key)]):
            groups[str(k)].append(r["r"])
    return {
        k: {"n": len(v), "win_rate": round(100 * sum(x > 0 for x in v) / len(v), 1),
            "avg_r": round(sum(v) / len(v), 3), "total_r": round(sum(v), 2)}
        for k, v in sorted(groups.items())
    }


def score_bucket(r: dict) -> list[str]:
    s = r["score"]
    return ["85+" if s >= 85 else "75-85" if s >= 75 else "70-75"]
