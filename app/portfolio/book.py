"""Paper book that follows daily target weights: mark to market, pay funding, rebalance, pay fees."""

from __future__ import annotations

from dataclasses import dataclass, field

FEE = 0.001  # taker fee + slippage per unit traded, same as the lab


@dataclass
class Book:
    equity: float
    positions: dict[str, float] = field(default_factory=dict)   # signed notional in USDT
    meta: dict[str, dict] = field(default_factory=dict)         # per position: entry price, day opened, side
    blocked: dict[str, tuple[int, int]] = field(default_factory=dict)  # after an emergency stop: symbol -> (side, until day)

    def mark(self, rets: dict[str, float], funding: dict[str, float], stopped: dict[str, float] | None = None) -> tuple[float, float]:
        """Apply one day's price change and funding to the current positions.
        stopped: symbol -> return from the previous close to the stop fill; those positions are closed."""
        price = fund = 0.0
        stopped = stopped or {}
        for s, n in list(self.positions.items()):
            r = stopped.get(s, rets.get(s))
            if r is None or r != r:  # no candle that day (delisted / halted): unchanged
                r = 0.0
            f = funding.get(s) or 0.0
            if f != f:
                f = 0.0
            price += n * r
            fund += n * f                 # longs pay positive funding, shorts receive it
            self.positions[s] = n * (1 + r)
        for s in stopped:                 # closed at the stop: pay the exit fee now
            n = self.positions.pop(s, 0.0)
            price -= FEE * abs(n)
            self.meta.pop(s, None)
        self.equity += price - fund
        return price, fund

    def check_stops(self, level: float, day: int, bars: dict[str, tuple], prev_close: dict[str, float],
                    cooldown_days: int = 7) -> dict[str, float]:
        """Emergency stop: close a position once price moved `level` (e.g. 0.4 = 40%) against its entry.
        bars: symbol -> (open, high, low) of the day. Fill at the stop, or at the open if it gapped past it."""
        out = {}
        for s, n in self.positions.items():
            m, b, pc = self.meta.get(s), bars.get(s), prev_close.get(s)
            if not m or not m.get("entry") or b is None or not pc or any(x != x for x in b):
                continue
            o, h, l_ = b
            if n > 0:
                stop = m["entry"] * (1 - level)
                fill = min(o, stop) if l_ <= stop else None
            else:
                stop = m["entry"] * (1 + level)
                fill = max(o, stop) if h >= stop else None
            if fill is not None:
                out[s] = fill / pc - 1
                self.blocked[s] = (1 if n > 0 else -1, day + cooldown_days * 86_400_000)
        return out

    def rebalance(self, weights: dict[str, float], allowed: set[str] | None = None,
                  min_notional: dict[str, float] | float = 0.0, fee: float = FEE, day: int | None = None) -> dict:
        """Move to target weights of current equity. allowed=None means every coin (ideal book)."""
        target, dropped = {}, 0
        for s, w in weights.items():
            if w == 0 or w != w:
                continue
            blk = self.blocked.get(s)
            if blk and day is not None:
                if day >= blk[1]:
                    del self.blocked[s]
                elif (w > 0) == (blk[0] > 0):  # no re-entry in the stopped direction during the cooldown
                    continue
            n = w * self.equity
            mn = min_notional.get(s, 5.0) if isinstance(min_notional, dict) else min_notional
            if (allowed is not None and s not in allowed) or abs(n) < mn:
                dropped += 1
                continue
            target[s] = n
        traded = sum(abs(target.get(s, 0.0) - self.positions.get(s, 0.0)) for s in set(target) | set(self.positions))
        fees = fee * traded
        self.equity -= fees
        before = dict(self.positions)
        self.positions = target
        return {"fees": fees, "turnover": traded, "dropped": dropped, "before": before}

    def update_meta(self, day: int, prices: dict[str, float]) -> None:
        """Remember when and at what price each position was opened (a flip counts as a new opening)."""
        for s, n in self.positions.items():
            side = 1 if n > 0 else -1
            m = self.meta.get(s)
            if m is None or m.get("side") != side:
                px = prices.get(s)
                self.meta[s] = {"entry": float(px) if px == px and px is not None else None, "since": int(day), "side": side}
        for s in [s for s in self.meta if s not in self.positions]:
            del self.meta[s]

    def snapshot(self, prices: dict[str, float], sources: dict[str, list] | None = None) -> dict:
        """What is saved per day: size, entry, opening day, that day's price and the styles behind it."""
        out = {}
        for s, n in self.positions.items():
            m = self.meta.get(s, {})
            px = prices.get(s)
            out[s] = {"n": round(n, 2), "entry": m.get("entry"), "since": m.get("since"),
                      "px": float(px) if px is not None and px == px else None, "src": (sources or {}).get(s, [])}
        return out

    def summary(self) -> dict:
        longs = sum(v for v in self.positions.values() if v > 0)
        shorts = -sum(v for v in self.positions.values() if v < 0)
        return {"n_long": sum(v > 0 for v in self.positions.values()), "n_short": sum(v < 0 for v in self.positions.values()),
                "gross": (longs + shorts) / self.equity if self.equity > 0 else 0.0,
                "net": (longs - shorts) / self.equity if self.equity > 0 else 0.0}
