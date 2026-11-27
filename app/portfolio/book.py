"""Paper book that follows daily target weights: mark to market, pay funding, rebalance, pay fees."""

from __future__ import annotations

from dataclasses import dataclass, field

FEE = 0.001  # taker fee + slippage per unit traded, same as the lab


@dataclass
class Book:
    equity: float
    positions: dict[str, float] = field(default_factory=dict)   # signed notional in USDT

    def mark(self, rets: dict[str, float], funding: dict[str, float]) -> tuple[float, float]:
        """Apply one day's price change and funding to the current positions."""
        price = fund = 0.0
        for s, n in list(self.positions.items()):
            r = rets.get(s)
            if r is None or r != r:  # no candle that day (delisted / halted): unchanged
                r = 0.0
            f = funding.get(s) or 0.0
            if f != f:
                f = 0.0
            price += n * r
            fund += n * f                 # longs pay positive funding, shorts receive it
            self.positions[s] = n * (1 + r)
        self.equity += price - fund
        return price, fund

    def rebalance(self, weights: dict[str, float], allowed: set[str] | None = None,
                  min_notional: dict[str, float] | float = 0.0, fee: float = FEE) -> dict:
        """Move to target weights of current equity. allowed=None means every coin (ideal book)."""
        target, dropped = {}, 0
        for s, w in weights.items():
            if w == 0 or w != w:
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

    def summary(self) -> dict:
        longs = sum(v for v in self.positions.values() if v > 0)
        shorts = -sum(v for v in self.positions.values() if v < 0)
        return {"n_long": sum(v > 0 for v in self.positions.values()), "n_short": sum(v < 0 for v in self.positions.values()),
                "gross": (longs + shorts) / self.equity if self.equity > 0 else 0.0,
                "net": (longs - shorts) / self.equity if self.equity > 0 else 0.0}
