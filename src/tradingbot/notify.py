"""Telegram notifications. Silent no-op when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set.

Setup (2 minutes):
  1. In Telegram talk to @BotFather -> /newbot -> copy the token
  2. Send any message to your new bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}
  3. Put both in .env and run:  python -m tradingbot notify-test
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = ""):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.prefix = prefix

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _post(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text[:4000],
                                       "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
                return bool(json.loads(r.read()).get("ok"))
        except Exception as e:  # never let a notification break trading
            log.warning("Telegram send failed: %s", type(e).__name__)
            return False

    def send(self, text: str, block: bool = False) -> bool:
        if not self.enabled:
            return False
        msg = f"{self.prefix} {text}".strip()
        if block:
            return self._post(msg)
        threading.Thread(target=self._post, args=(msg,), daemon=True).start()
        return True
