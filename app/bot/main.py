"""Telegram bot service: account linking and signal notifications.

Run with:  python -m app.bot.main
Uses the Bot API directly (long polling), no extra framework.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import httpx

from app.bot import messages as M
from app.config import load_settings
from app.data.store import Store

log = logging.getLogger("telec.bot")
DAILY_CAP = int(os.environ.get("DAILY_SIGNAL_CAP", "10"))


class TelegramError(RuntimeError):
    def __init__(self, code: int, desc: str):
        super().__init__(f"{code}: {desc}")
        self.code = code


class Bot:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=70)

    async def call(self, method: str, **params):
        for attempt in range(3):
            r = await self.http.post(f"{self.base}/{method}", json=params)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429:
                await asyncio.sleep(int(data.get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            raise TelegramError(r.status_code, data.get("description", "error"))
        raise TelegramError(429, "rate limited")

    async def send(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                        disable_web_page_preview=True, protect_content=True)

    async def close(self) -> None:
        await self.http.aclose()


class BotService:
    def __init__(self, store: Store, bot: Bot, site: str):
        self.s, self.bot, self.site = store, bot, site
        self.stop = asyncio.Event()

    # ---- incoming ------------------------------------------------------
    async def handle(self, msg: dict) -> None:
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return
        chat_id, text = chat["id"], (msg.get("text") or "").strip()
        name = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name") or ""
        cmd, _, arg = text.partition(" ")
        async with self.s.pool.acquire() as con:
            if cmd == "/start" and arg:
                user = await con.fetchrow(
                    "SELECT id, username FROM users WHERE tg_link_code=$1 AND tg_link_expires > now() AND active", arg.strip())
                if not user:
                    await self.bot.send(chat_id, "لینک اتصال نامعتبر یا منقضی است. از سایت لینک تازه بگیرید.")
                    return
                await con.execute("UPDATE users SET telegram_chat_id=NULL WHERE telegram_chat_id=$1", chat_id)
                await con.execute(
                    "UPDATE users SET telegram_chat_id=$2, telegram_name=$3, tg_link_code=NULL, notify=true WHERE id=$1",
                    user["id"], chat_id, name)
                await self.bot.send(chat_id, M.welcome_linked(user["username"]))
                return
            user = await con.fetchrow("SELECT id, username, notify FROM users WHERE telegram_chat_id=$1 AND active", chat_id)
            if user is None:
                await self.bot.send(chat_id, M.help_text(self.site))
                return
            if cmd in ("/on", "/off"):
                await con.execute("UPDATE users SET notify=$2 WHERE id=$1", user["id"], cmd == "/on")
                await self.bot.send(chat_id, "اعلان‌ها روشن شد." if cmd == "/on" else "اعلان‌ها خاموش شد. با /on دوباره روشن می‌شود.")
            elif cmd == "/status":
                active = await con.fetch("SELECT symbol, side, tp1_hit FROM signal_candidates WHERE status='active' ORDER BY bar_time")
                today = await con.fetchval("SELECT count(*) FROM signal_candidates WHERE published AND created_at > now() - interval '24 hours'")
                lines = [f"حساب: <b>{user['username']}</b>   اعلان‌ها: {'روشن' if user['notify'] else 'خاموش'}",
                         f"سیگنال‌های ۲۴ ساعت اخیر: {today}", "", "معاملات باز:" if active else "معامله بازی نیست."]
                lines += [f"• {'خرید' if a['side'] > 0 else 'فروش'} {a['symbol']}{' (TP1 گرفته شد)' if a['tp1_hit'] else ''}" for a in active]
                await self.bot.send(chat_id, "\n".join(lines))
            else:
                await self.bot.send(chat_id, M.help_text(self.site))

    async def poll_loop(self) -> None:
        offset = None
        while not self.stop.is_set():
            try:
                params = {"timeout": 50, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                for upd in await self.bot.call("getUpdates", **params):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        try:
                            await self.handle(upd["message"])
                        except Exception:
                            log.exception("handling message failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("getUpdates: %s", exc)
                await asyncio.sleep(5)

    # ---- outgoing ------------------------------------------------------
    async def recipients(self, con) -> list[int]:
        return [r["telegram_chat_id"] for r in await con.fetch(
            "SELECT telegram_chat_id FROM users WHERE active AND notify AND telegram_chat_id IS NOT NULL")]

    async def broadcast(self, con, text: str) -> None:
        for chat_id in await self.recipients(con):
            try:
                await self.bot.send(chat_id, text)
            except TelegramError as exc:
                if exc.code in (400, 403):  # blocked the bot or chat gone
                    await con.execute("UPDATE users SET notify=false WHERE telegram_chat_id=$1", chat_id)
                log.warning("send to %s failed: %s", chat_id, exc)
            await asyncio.sleep(0.05)

    async def notify_once(self) -> int:
        sent = 0
        async with self.s.pool.acquire() as con:
            events = await con.fetch(
                """SELECT e.id AS event_id, e.kind AS event_kind, e.payload AS event_payload, c.*
                   FROM signal_events e JOIN signal_candidates c ON c.id=e.candidate_id
                   WHERE e.sent_at IS NULL ORDER BY e.id LIMIT 50""")
            for e in events:
                c = dict(e)
                c["styles"] = json.loads(c["styles"]) if isinstance(c["styles"], str) else c["styles"]
                c["notes"] = json.loads(c["notes"]) if isinstance(c["notes"], str) else c["notes"]
                payload = json.loads(e["event_payload"]) if isinstance(e["event_payload"], str) else e["event_payload"]
                kind = e["event_kind"]
                text = None
                if kind == "new":
                    published_today = await con.fetchval(
                        "SELECT count(*) FROM signal_candidates WHERE published AND created_at > now() - interval '24 hours'")
                    publish = published_today < DAILY_CAP
                    await con.execute("UPDATE signal_candidates SET published=$2 WHERE id=$1", c["id"], publish)
                    if publish:
                        text = M.new_signal(c, self.site)
                elif c.get("published"):
                    text = M.update(c, kind, payload)
                if text:
                    await self.broadcast(con, text)
                    sent += 1
                await con.execute("UPDATE signal_events SET sent_at=now() WHERE id=$1", e["event_id"])
            # plain messages queued by other services (daily portfolio summary)
            for m in await con.fetch("SELECT id, text FROM bot_outbox WHERE sent_at IS NULL AND created_at > now() - interval '2 days' ORDER BY id LIMIT 10"):
                await self.broadcast(con, m["text"])
                await con.execute("UPDATE bot_outbox SET sent_at=now() WHERE id=$1", m["id"])
                sent += 1
        return sent

    async def notify_loop(self) -> None:
        while not self.stop.is_set():
            try:
                n = await self.notify_once()
                if n:
                    log.info("sent %d notifications", n)
            except Exception:
                log.exception("notify failed")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass


async def main() -> None:
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.admin_bot_token
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set in .env; bot not started")
        while True:  # stay up quietly so systemd does not restart-loop
            await asyncio.sleep(3600)
    site = "https://" + os.environ.get("DOMAIN", "signal.telec.shop")
    store = Store(cfg.database_url)
    await store.connect()
    await store.apply_schema()
    bot = Bot(token)
    me = await bot.call("getMe")
    async with store.pool.acquire() as con:
        await con.execute("INSERT INTO app_settings (key, value) VALUES ('bot_username', $1) ON CONFLICT (key) DO UPDATE SET value=$1",
                          me["username"])
        # do not flood new subscribers with a backlog from before the bot was running
        stale = await con.execute("UPDATE signal_events SET sent_at=now() WHERE sent_at IS NULL AND created_at < now() - interval '2 hours'")
    log.info("bot @%s started (%s stale events skipped)", me["username"], stale.split()[-1])
    svc = BotService(store, bot, site)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, svc.stop.set)
    tasks = [asyncio.create_task(svc.poll_loop()), asyncio.create_task(svc.notify_loop())]
    await svc.stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bot.close()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
