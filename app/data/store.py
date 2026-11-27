"""PostgreSQL access (asyncpg). Timestamps are stored as timestamptz; the app uses epoch ms."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from app.core.models import Candle, UniverseEntry

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Store:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self.pool = None

    async def connect(self, attempts: int = 30, delay_s: float = 2.0) -> None:
        """Retry while PostgreSQL is still starting (e.g. right after a restart)."""
        import asyncio

        import asyncpg  # lazy: tests import this module without asyncpg

        for i in range(1, attempts + 1):
            try:
                self.pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5, command_timeout=60)
                return
            except (OSError, asyncpg.CannotConnectNowError) as exc:
                if i == attempts:
                    raise RuntimeError(f"PostgreSQL unreachable after {attempts} attempts: {exc}") from exc
                log.warning("database not ready (%s), retry %d/%d", exc, i, attempts)
                await asyncio.sleep(delay_s)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def apply_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        async with self.pool.acquire() as con:
            await con.execute(sql)

    # ---- candles -------------------------------------------------------
    async def upsert_candles(self, candles: list[Candle]) -> int:
        if not candles:
            return 0
        rows = [
            (c.symbol, c.tf, ms_to_dt(c.open_time), c.open, c.high, c.low, c.close, c.volume, c.quote_volume, c.trades)
            for c in candles
        ]
        async with self.pool.acquire() as con:
            await con.executemany(
                """
                INSERT INTO candles (symbol, tf, open_time, open, high, low, close, volume, quote_volume, trades)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (symbol, tf, open_time) DO UPDATE SET
                    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                    volume=EXCLUDED.volume, quote_volume=EXCLUDED.quote_volume, trades=EXCLUDED.trades,
                    received_at=now()
                """,
                rows,
            )
        return len(rows)

    async def last_open_time(self, symbol: str, tf: str) -> int | None:
        async with self.pool.acquire() as con:
            v = await con.fetchval("SELECT max(open_time) FROM candles WHERE symbol=$1 AND tf=$2", symbol, tf)
        return dt_to_ms(v) if v else None

    async def open_times(self, symbol: str, tf: str, start_ms: int, end_ms: int) -> list[int]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT open_time FROM candles WHERE symbol=$1 AND tf=$2 AND open_time BETWEEN $3 AND $4",
                symbol, tf, ms_to_dt(start_ms), ms_to_dt(end_ms),
            )
        return [dt_to_ms(r["open_time"]) for r in rows]

    async def latest_received(self) -> datetime | None:
        async with self.pool.acquire() as con:
            return await con.fetchval("SELECT max(received_at) FROM candles WHERE received_at > now() - interval '1 day'")

    # ---- universe ------------------------------------------------------
    async def save_universe(self, day: date, universe: list[UniverseEntry], analysis: list[str]) -> None:
        chosen = set(analysis)
        rows = [
            (day, i + 1, u.symbol, u.base, u.multiplier, u.quote_volume_24h, u.range_pct_24h, u.on_lbank, u.lbank_symbol, u.symbol in chosen)
            for i, u in enumerate(universe)
        ]
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("DELETE FROM universe_snapshots WHERE day=$1", day)
                await con.executemany(
                    """INSERT INTO universe_snapshots
                       (day, rank, symbol, base, multiplier, quote_volume_24h, range_pct_24h, on_lbank, lbank_symbol, in_analysis)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    rows,
                )

    # ---- admin-added coins ---------------------------------------------
    async def custom_inputs(self) -> list[str]:
        async with self.pool.acquire() as con:
            return [r["input"] for r in await con.fetch("SELECT input FROM custom_symbols ORDER BY added_at")]

    async def set_custom_status(self, status: dict) -> None:
        async with self.pool.acquire() as con:
            for inp, (st, sym, note) in status.items():
                await con.execute(
                    "UPDATE custom_symbols SET status=$2, symbol=$3, note=$4, checked_at=now() WHERE input=$1", inp, st, sym, note)

    # ---- derivatives data ---------------------------------------------
    async def save_funding(self, rows: list[tuple[str, int, float, float]]) -> None:
        """rows: (symbol, ts_ms, rate, mark_price)"""
        if not rows:
            return
        async with self.pool.acquire() as con:
            await con.executemany(
                """INSERT INTO funding_rates (symbol, ts, rate, mark_price) VALUES ($1,$2,$3,$4)
                   ON CONFLICT (symbol, ts) DO UPDATE SET rate=EXCLUDED.rate, mark_price=EXCLUDED.mark_price""",
                [(s, ms_to_dt(t), r, p) for s, t, r, p in rows],
            )

    async def save_open_interest(self, rows: list[tuple[str, int, float]]) -> None:
        if not rows:
            return
        async with self.pool.acquire() as con:
            await con.executemany(
                "INSERT INTO open_interest (symbol, ts, oi) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                [(s, ms_to_dt(t), oi) for s, t, oi in rows],
            )

    # ---- issues & retention -------------------------------------------
    async def log_issue(self, kind: str, symbol: str | None = None, tf: str | None = None, detail: dict | None = None) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "INSERT INTO data_issues (symbol, tf, kind, detail) VALUES ($1,$2,$3,$4::jsonb)",
                symbol, tf, kind, json.dumps(detail or {}),
            )

    async def prune(self, tf: str, keep_days: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "DELETE FROM candles WHERE tf=$1 AND open_time < now() - make_interval(days => $2)", tf, keep_days
            )
