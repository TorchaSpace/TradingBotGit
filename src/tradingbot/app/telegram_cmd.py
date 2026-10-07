"""Control the running app from Telegram (like Freqtrade's /status, /stop, /forceexit).

Only messages from the configured TELEGRAM_CHAT_ID are obeyed; everything else is ignored.
Commands:
  /durum           engines, account mode, equity, open positions
  /dur             stop all engines (open positions keep their exchange stops)
  /kapat           ask to close ALL bot positions -> answer /kapat_onay within 2 minutes
  /yardim          this list
Starting engines from Telegram is deliberately NOT possible (and nothing can switch to the real account).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
HELP = ("Komutlar:\n/durum - botların durumu, özsermaye, açık pozisyonlar\n/dur - tüm motorları durdur "
        "(stop emirleri borsada kalır)\n/kapat - tüm bot pozisyonlarını kapat (onay ister)\n/yardim - bu liste")
CONFIRM_SECONDS = 120


class TelegramCommands:
    def __init__(self, token: str, chat_id: str, engines, status_text, api=None):
        self.token, self.chat_id = token.strip(), str(chat_id).strip()
        self.engines = engines              # EngineManager
        self.status_text = status_text      # () -> str
        self.api = api or self._api
        self._stop = threading.Event()
        self._offset = None
        self._confirm_until = 0.0
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ transport
    def _api(self, method: str, params: dict, timeout: float = 35) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.loads(r.read())

    def reply(self, text: str) -> None:
        try:
            self.api("sendMessage", {"chat_id": self.chat_id, "text": text[:4000]}, 10)
        except Exception as e:
            log.warning("Telegram yanıtı gönderilemedi: %s", type(e).__name__)

    # ------------------------------------------------------------------ loop
    def start(self) -> "TelegramCommands":
        self.thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:  # skip messages sent before the app started
            r = self.api("getUpdates", {"timeout": 0, "offset": -1}, 15)
            res = r.get("result") or []
            self._offset = res[-1]["update_id"] + 1 if res else None
        except Exception:
            self._offset = None
        log.info("Telegram komutları dinleniyor (/yardim)")
        while not self._stop.is_set():
            try:
                params = {"timeout": 25}
                if self._offset is not None:
                    params["offset"] = self._offset
                r = self.api("getUpdates", params, 35)
                for u in r.get("result") or []:
                    self._offset = u["update_id"] + 1
                    self.handle(u)
            except Exception as e:
                log.debug("Telegram getUpdates: %s", e)
                self._stop.wait(15)

    # ------------------------------------------------------------------ commands
    def handle(self, update: dict) -> str | None:
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return None                        # not the owner: ignore silently
        cmd = (msg.get("text") or "").strip().split()[0].split("@")[0].lower() if msg.get("text") else ""
        if cmd in ("/durum", "/status"):
            out = self.status_text()
        elif cmd in ("/dur", "/stop"):
            stopped = [k for k in self.engines.KINDS if self.engines.running(k)]
            for k in stopped:
                self.engines.stop(k, wait=20)
            out = ("Durduruldu: " + ", ".join(stopped) + ". Açık pozisyonların stop emirleri borsada duruyor."
                   if stopped else "Zaten çalışan motor yok.")
            log.warning("Telegram'dan durduruldu: %s", stopped)
        elif cmd == "/kapat":
            self._confirm_until = time.time() + CONFIRM_SECONDS
            out = "Tüm bot pozisyonları piyasa fiyatından kapatılacak. Onaylamak için 2 dakika içinde /kapat_onay yaz."
        elif cmd == "/kapat_onay":
            if time.time() > self._confirm_until:
                out = "Onay süresi doldu ya da önce /kapat yazılmadı."
            else:
                self._confirm_until = 0.0
                res = []
                for k in self.engines.KINDS:
                    if self.engines.running(k):
                        self.engines.stop(k, wait=20)
                    ok, m = self.engines.flatten(k) if k != "carry" or self._has_carry() else (True, "yok")
                    res.append(f"{k}: {m}")
                out = "Kapatıldı.\n" + "\n".join(res)
                log.warning("Telegram'dan tüm pozisyonlar kapatıldı")
        elif cmd in ("/yardim", "/help", "/start"):
            out = HELP
        else:
            return None
        self.reply(out)
        return out

    def _has_carry(self) -> bool:
        try:
            return self.engines.settings().carry_capital > 0
        except Exception:
            return False
