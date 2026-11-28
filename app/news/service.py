"""News collector (service telec-news): RSS feeds, Binance delisting notices, economic calendar.

  .venv/bin/python -m app.news.service          # loop
  .venv/bin/python -m app.news.service --once   # one round, prints a summary
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import load_settings
from app.data.store import Store
from app.news.classify import (BINANCE_DELIST_URL, CALENDAR_URLS, CAT_LABEL, FEEDS, category, coins, importance,
                               parse_binance_delist, parse_feed, same_story, tokens)

log = logging.getLogger("telec.news")
FEED_EVERY_S = 5 * 60
CALENDAR_EVERY_S = 6 * 3600
MAX_ALERTS_PER_DAY = 6
UA = "telec-signal news reader (admin@telec.shop)"


class NewsService:
    def __init__(self, store: Store):
        self.store = store
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=True,
                                      headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
        self.stop = asyncio.Event()
        self.last_calendar = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def known_tickers(self) -> set[str]:
        async with self.store.pool.acquire() as con:
            rows = await con.fetch("SELECT DISTINCT symbol FROM candles WHERE tf='1d' AND open_time > now() - interval '3 days'")
        out = set()
        for r in rows:
            b = r["symbol"][:-4]
            for p in ("1000000", "1000", "100"):
                if b.startswith(p) and len(b) > len(p):
                    b = b[len(p):]
            out.add(b)
        return out

    async def held(self) -> set[str]:
        """Bases held by the executable portfolio books."""
        async with self.store.pool.acquire() as con:
            rows = await con.fetch("SELECT positions FROM portfolio_books WHERE mode='exec'")
        out = set()
        for r in rows:
            pos = json.loads(r["positions"]) if isinstance(r["positions"], str) else (r["positions"] or {})
            for s in pos:
                b = s[:-4]
                for p in ("1000000", "1000", "100"):
                    if b.startswith(p) and len(b) > len(p):
                        b = b[len(p):]
                out.add(b)
        return out

    async def _status(self, name: str, ok: bool, n: int = 0, err: str | None = None) -> None:
        async with self.store.pool.acquire() as con:
            await con.execute(
                """INSERT INTO news_sources (name, last_ok, last_error, last_error_at, items)
                   VALUES ($1, CASE WHEN $2 THEN now() END, $3, CASE WHEN $2 THEN NULL ELSE now() END, $4)
                   ON CONFLICT (name) DO UPDATE SET
                     last_ok = CASE WHEN $2 THEN now() ELSE news_sources.last_ok END,
                     last_error = CASE WHEN $2 THEN news_sources.last_error ELSE $3 END,
                     last_error_at = CASE WHEN $2 THEN news_sources.last_error_at ELSE now() END,
                     items = news_sources.items + $4""", name, ok, err, n)

    async def fetch_all(self) -> list[tuple[str, str, dict]]:
        """(source, group, item) for every item currently in the feeds."""
        out = []

        async def one(name, group, url):
            try:
                r = await self.http.get(url)
                r.raise_for_status()
                items = parse_feed(r.content)
                await self._status(name, True)
                return [(name, group, it) for it in items]
            except Exception as exc:
                await self._status(name, False, err=str(exc)[:200])
                log.warning("feed %s: %s", name, exc)
                return []

        results = await asyncio.gather(*(one(*f) for f in FEEDS))
        for res in results:
            out.extend(res)
        try:
            r = await self.http.get(BINANCE_DELIST_URL, headers={"Accept": "application/json"})
            r.raise_for_status()
            items = parse_binance_delist(r.json())
            await self._status("Binance (حذف)", True)
            out.extend(("Binance (حذف)", "official", it) for it in items)
        except Exception as exc:
            await self._status("Binance (حذف)", False, err=str(exc)[:200])
        return out

    async def store_items(self, items: list[tuple[str, str, dict]]) -> list[dict]:
        """Insert new items (by URL), classify them, attach them to a story cluster. Returns the new ones."""
        known, held = await self.known_tickers(), await self.held()
        now = datetime.now(timezone.utc)
        new = []
        async with self.store.pool.acquire() as con:
            urls = [it["url"] for _, _, it in items]
            seen = {r["url"] for r in await con.fetch("SELECT url FROM news_items WHERE url = ANY($1::text[])", urls)}
            recent = await con.fetch(
                "SELECT id, cluster_id, title, source FROM news_items WHERE published_at > now() - interval '36 hours'")
            pool = [(r["cluster_id"], tokens(r["title"]), r["source"]) for r in recent]
            for source, group, it in items:
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                pub = it["published"] or now
                if pub > now + timedelta(hours=1) or pub < now - timedelta(days=7):
                    continue  # bad dates or old backlog
                cat, base = category(it["title"], it["summary"], group)
                if source.startswith("Binance"):
                    cat, base = "delist", 3
                cl = coins(it["title"], it["summary"], known)
                tk = tokens(it["title"])
                cluster = next((c for c, t, s in pool if s != source and same_story(tk, t)), None)
                row = await con.fetchrow(
                    """INSERT INTO news_items (source, source_group, url, title, summary, published_at, category, coins,
                                               importance, cluster_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,1,$9) RETURNING id""",
                    source, group, it["url"], it["title"], it["summary"], pub, cat, cl, cluster)
                cid = cluster or row["id"]
                if cluster is None:
                    await con.execute("UPDATE news_items SET cluster_id=id WHERE id=$1", row["id"])
                pool.append((cid, tk, source))
                n_sources = await con.fetchval("SELECT count(DISTINCT source) FROM news_items WHERE cluster_id=$1", cid)
                imp = importance(base, cl, held, n_sources >= 2, group == "official", it["title"])
                await con.execute("UPDATE news_items SET importance=$2 WHERE id=$1", row["id"], imp)
                if n_sources >= 2:  # a second source confirms the story: raise the whole cluster
                    await con.execute("UPDATE news_items SET importance=LEAST(5, importance+1) WHERE cluster_id=$1 AND id<>$2 "
                                      "AND NOT verified_bump", cid, row["id"])
                    await con.execute("UPDATE news_items SET verified_bump=true WHERE cluster_id=$1", cid)
                new.append({"id": row["id"], "source": source, "title": it["title"], "category": cat, "coins": cl,
                            "importance": imp, "url": it["url"], "held": sorted(set(cl) & held), "cluster": cid})
        return new

    async def alert(self, new: list[dict]) -> int:
        """Telegram only for what matters: a delisting of a held coin, or a top-importance confirmed story."""
        sent = 0
        async with self.store.pool.acquire() as con:
            today = await con.fetchval("SELECT count(*) FROM news_items WHERE alerted_at > now() - interval '1 day'")
            for n in new:
                urgent = (n["category"] == "delist" and n["held"]) or n["importance"] >= 5
                if not urgent or today + sent >= MAX_ALERTS_PER_DAY:
                    continue
                done = await con.fetchval("SELECT count(*) FROM news_items WHERE cluster_id=$1 AND alerted_at IS NOT NULL", n["cluster"])
                if done:
                    continue
                held = f"\nدر پرتفوی: {'، '.join(n['held'])}" if n["held"] else ""
                text = (f"📰 <b>{CAT_LABEL.get(n['category'], '')}</b> — {n['source']}\n{_esc(n['title'])}{held}\n"
                        f"<a href=\"{_esc(n['url'])}\">متن خبر</a>")
                await con.execute("INSERT INTO bot_outbox (text) VALUES ($1)", text)
                await con.execute("UPDATE news_items SET alerted_at=now() WHERE id=$1", n["id"])
                sent += 1
        return sent

    async def calendar(self) -> int:
        events = []
        for url in CALENDAR_URLS:
            try:
                r = await self.http.get(url, headers={"Accept": "application/json"})
                r.raise_for_status()
                events.extend(r.json())
                await self._status("تقویم اقتصادی", True)
            except Exception as exc:
                await self._status("تقویم اقتصادی", False, err=str(exc)[:200])
        rows = []
        for e in events:
            if e.get("impact") not in ("High", "Medium"):
                continue
            try:
                ts = datetime.fromisoformat(str(e["date"]))
            except (KeyError, ValueError):
                continue
            rows.append((str(e.get("title", ""))[:200], str(e.get("country", ""))[:8], e["impact"], ts,
                         str(e.get("forecast") or "")[:40], str(e.get("previous") or "")[:40], str(e.get("actual") or "")[:40]))
        if rows:
            async with self.store.pool.acquire() as con:
                await con.executemany(
                    """INSERT INTO econ_events (title, country, impact, ts, forecast, previous, actual) VALUES ($1,$2,$3,$4,$5,$6,$7)
                       ON CONFLICT (title, country, ts) DO UPDATE SET impact=$3, forecast=$5, previous=$6,
                         actual=CASE WHEN $7 <> '' THEN $7 ELSE econ_events.actual END""", rows)
        return len(rows)

    async def round(self) -> dict:
        async with self.store.pool.acquire() as con:
            first = not await con.fetchval("SELECT EXISTS (SELECT 1 FROM news_items)")
        items = await self.fetch_all()
        new = await self.store_items(items)
        alerts = 0 if first else await self.alert(new)  # the first round is backlog: no Telegram flood
        cal = None
        if time.time() - self.last_calendar >= CALENDAR_EVERY_S:
            cal = await self.calendar()
            self.last_calendar = time.time()
        async with self.store.pool.acquire() as con:
            await con.execute("DELETE FROM news_items WHERE published_at < now() - interval '90 days'")
        return {"fetched": len(items), "new": len(new), "alerts": alerts, "calendar": cal, "items": new}

    async def loop(self) -> None:
        while not self.stop.is_set():
            try:
                r = await self.round()
                log.info("news: %d fetched, %d new, %d alerts", r["fetched"], r["new"], r["alerts"])
            except Exception:
                log.exception("news round failed")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=FEED_EVERY_S)
            except asyncio.TimeoutError:
                pass


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(cfg.database_url)
    await store.connect()
    await store.apply_schema()
    svc = NewsService(store)
    try:
        if a.once:
            r = await svc.round()
            print(f"{r['fetched']} items in feeds, {r['new']} new, {r['alerts']} alerts, calendar events {r['calendar']}")
            async with store.pool.acquire() as con:
                for s in await con.fetch("""SELECT name, last_error,
                                              coalesce(last_ok > coalesce(last_error_at, 'epoch'), false) AS ok
                                            FROM news_sources ORDER BY name"""):
                    print(f"  {'OK ' if s['ok'] else '!! '}{s['name']}" + ("" if s["ok"] else f": {s['last_error']}"))
            for n in sorted(r["items"], key=lambda x: -x["importance"])[:15]:
                print(f"  [{n['importance']}] {n['category']:<10} {n['source']:<14} {n['title'][:90]} {n['coins'] or ''}")
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, svc.stop.set)
        await svc.loop()
    finally:
        await svc.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
