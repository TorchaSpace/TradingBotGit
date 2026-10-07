"""Telegram commands: only the owner's chat is obeyed; /kapat needs a confirmation."""
from tradingbot.app.telegram_cmd import TelegramCommands


class Eng:
    KINDS = ("trend", "carry", "ml")

    def __init__(self):
        self.run = {"trend": True, "carry": False, "ml": True}
        self.flat = []

    def running(self, k):
        return self.run[k]

    def stop(self, k, wait=0):
        self.run[k] = False

    def flatten(self, k):
        self.flat.append(k)
        return True, "pozisyonlar kapatıldı"

    def settings(self):
        return type("S", (), {"carry_capital": 0})()


def msg(text, chat="42"):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def test_commands():
    sent = []
    e = Eng()
    t = TelegramCommands("tok", "42", e, lambda: "durum yazısı", api=lambda m, p, timeout=0: sent.append((m, p)) or {})
    assert t.handle(msg("/durum", chat="999")) is None and not sent      # stranger ignored
    assert t.handle(msg("/durum")) == "durum yazısı"
    assert t.handle(msg("/kapat_onay")).startswith("Onay süresi")          # no /kapat first
    assert e.flat == []
    t.handle(msg("/kapat"))
    out = t.handle(msg("/kapat_onay"))
    assert "Kapatıldı" in out and set(e.flat) == {"trend", "ml"} and not any(e.run.values())
    e.run["trend"] = True
    assert "trend" in t.handle(msg("/dur")) and not e.run["trend"]
    assert t.handle(msg("merhaba")) is None
    assert all(p["chat_id"] == "42" for m, p in sent)
