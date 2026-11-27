"""Admin alerts to Telegram, de-duplicated so one problem does not flood the chat."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class AdminAlerter:
    def __init__(self, bot_token: str, chat_id: str, cooldown_s: int = 1800):
        self._token = bot_token
        self._chat = chat_id
        self._cooldown = cooldown_s
        self._last: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat)

    async def send(self, key: str, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last.get(key, -1e9) < self._cooldown:
            return
        self._last[key] = now
        log.warning("ALERT[%s] %s", key, text)
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat, "text": f"⚠️ telec-signal\n{text}"},
                )
        except Exception as exc:  # alerts must never crash the engine
            log.error("telegram alert failed: %s", exc)
