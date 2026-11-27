"""Paper-trading core: position sizing and candle-by-candle trade simulation.

Pure functions, no I/O. Rules (agreed for the paper account):
  * fill at the OPEN of the 15m bar after the signal bar (no look-ahead)
  * risk % by score: >=85 -> 1.5%, >=75 -> 1%, else 0.5% of equity
  * leverage is the lowest that fits the margin budget, capped at 20x and kept
    low enough that liquidation sits well beyond the stop
  * TP1 closes half and moves the stop to entry; TP2 closes the rest
  * if one candle touches both the stop and a target, the stop wins (conservative)
  * taker fee on every fill; funding charged at 00/08/16 UTC while open
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MAX_LEVERAGE = 20
MAINT_MARGIN = 0.005  # approx. maintenance margin rate
LIQ_BUFFER = 1.5  # liquidation distance must exceed 1.5x stop distance
TP1_FRACTION = 0.5
FUNDING_HOURS = (0, 8, 16)
HOUR_MS = 3_600_000


def risk_pct_for_score(score: float) -> float:
    if score >= 85:
        return 1.5
    if score >= 75:
        return 1.0
    return 0.5


@dataclass
class Sizing:
    qty: float
    leverage: int
    margin: float
    notional: float
    risk_pct: float
    risk_amount: float
    note: str = ""


def size_position(equity: float, free_margin: float, slots_left: int, score: float, entry: float, stop: float, fee_rate: float) -> Sizing | None:
    """Returns None when the trade cannot be placed (reason in the caller)."""
    dist = abs(entry - stop)
    if equity <= 0 or dist <= 0 or entry <= 0 or slots_left <= 0:
        return None
    rp = risk_pct_for_score(score)
    risk_amount = equity * rp / 100
    # include entry+exit fees in the loss at the stop
    qty = risk_amount / (dist + 2 * entry * fee_rate)
    notional = qty * entry
    # highest leverage that keeps liquidation beyond the stop with a buffer
    lev_safe = max(1, int(entry / (LIQ_BUFFER * dist + entry * MAINT_MARGIN)))
    lev_cap = min(MAX_LEVERAGE, lev_safe)
    budget = free_margin / slots_left  # share the free margin between open slots
    note = ""
    lev = max(1, math.ceil(notional / budget)) if budget > 0 else lev_cap + 1
    if lev > lev_cap:
        # not enough margin at a safe leverage: shrink the position
        qty = budget * lev_cap / entry
        notional = qty * entry
        lev = lev_cap
        note = "حجم به‌خاطر محدودیت مارجین کم شد"
        if qty <= 0:
            return None
    margin = notional / lev
    return Sizing(qty=qty, leverage=lev, margin=margin, notional=notional, risk_pct=rp,
                  risk_amount=qty * (dist + 2 * entry * fee_rate), note=note)


@dataclass
class Trade:
    side: int
    entry: float
    qty: float
    stop: float
    tp1: float
    tp2: float
    fee_rate: float
    qty_open: float = 0.0
    tp1_hit: bool = False
    realized: float = 0.0  # price PnL of closed parts, after fees and funding
    fees: float = 0.0
    funding: float = 0.0
    closed: bool = False
    exit_reason: str | None = None
    events: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.qty_open == 0.0 and not self.closed:
            self.qty_open = self.qty

    def open_fee(self, t_ms: int) -> None:
        fee = self.qty * self.entry * self.fee_rate
        self.fees += fee
        self.realized -= fee
        self.events.append({"t": t_ms, "type": "open", "price": self.entry, "qty": self.qty, "fee": fee})

    def _close(self, qty: float, price: float, t_ms: int, kind: str) -> None:
        qty = min(qty, self.qty_open)
        pnl = self.side * (price - self.entry) * qty
        fee = qty * price * self.fee_rate
        self.realized += pnl - fee
        self.fees += fee
        self.qty_open -= qty
        self.events.append({"t": t_ms, "type": kind, "price": price, "qty": qty, "pnl": pnl, "fee": fee})
        if self.qty_open <= self.qty * 1e-9:
            self.qty_open = 0.0
            self.closed = True
            self.exit_reason = kind

    def on_candle(self, t_ms: int, high: float, low: float) -> None:
        """Process one closed candle (open time t_ms)."""
        if self.closed:
            return
        long = self.side == 1
        hit_stop = low <= self.stop if long else high >= self.stop
        if hit_stop:  # stop first: conservative when the candle also touches a target
            self._close(self.qty_open, self.stop, t_ms, "breakeven" if self.tp1_hit else "stop")
            return
        hit_tp1 = high >= self.tp1 if long else low <= self.tp1
        hit_tp2 = high >= self.tp2 if long else low <= self.tp2
        if not self.tp1_hit and hit_tp1:
            self._close(self.qty * TP1_FRACTION, self.tp1, t_ms, "tp1")
            self.tp1_hit = True
            self.stop = self.entry
        if self.tp1_hit and hit_tp2 and not self.closed:
            self._close(self.qty_open, self.tp2, t_ms, "tp2")

    def apply_funding(self, t_ms: int, rate: float, mark: float) -> None:
        if self.closed or self.qty_open <= 0:
            return
        cost = self.side * self.qty_open * mark * rate  # longs pay positive funding
        self.funding += cost
        self.realized -= cost
        self.events.append({"t": t_ms, "type": "funding", "rate": rate, "cost": cost})

    def unrealized(self, price: float) -> float:
        return self.side * (price - self.entry) * self.qty_open


def funding_times(start_ms: int, end_ms: int) -> list[int]:
    """Funding settlement times in (start, end]."""
    out = []
    t = start_ms - start_ms % HOUR_MS + HOUR_MS
    while t <= end_ms:
        if (t // HOUR_MS) % 24 in FUNDING_HOURS:
            out.append(t)
        t += HOUR_MS
    return out


def stats(closed_pnls: list[float], equity_curve: list[float]) -> dict:
    n = len(closed_pnls)
    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    peak, max_dd = -math.inf, 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    return {
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1) if n else None,
        "net_pnl": round(sum(closed_pnls), 2),
        "expectancy": round(sum(closed_pnls) / n, 2) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "max_drawdown_pct": round(100 * max_dd, 2),
    }
