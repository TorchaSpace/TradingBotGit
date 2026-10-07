"""Runs the trend bot and the carry engine in background threads for the desktop app."""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
from datetime import datetime, timezone

from ..config import LIVE_CONFIRM_PHRASE, PROJECT_ROOT, ConfigError, Settings, settings_from
from ..envfile import read_env
from .schema import SECRET_KEYS

STATE_DIR = PROJECT_ROOT / "state"
SESSIONS_FILE = STATE_DIR / "sessions.json"
log = logging.getLogger("tradingbot.app")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LogBuffer(logging.Handler):
    """Keeps the last N log records in memory for the app's Output panel."""

    def __init__(self, size: int = 5000):
        super().__init__(level=logging.INFO)
        self.records: collections.deque = collections.deque(maxlen=size)
        self._id = 0
        self._lock = threading.Lock()
        self.session = ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info).splitlines()[-1]
        except Exception:
            msg = str(record.msg)
        with self._lock:
            self._id += 1
            self.records.append({"id": self._id, "time": datetime.fromtimestamp(record.created, timezone.utc)
                                 .isoformat(timespec="seconds"), "level": record.levelname,
                                 "source": record.name.replace("tradingbot.", ""), "msg": msg,
                                 "session": self.session})

    def since(self, after: int, limit: int = 500) -> list[dict]:
        with self._lock:
            return [r for r in self.records if r["id"] > after][-limit:]


def redacted_settings(env: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in env.items():
        if k in SECRET_KEYS and v:
            out[k] = f"•••{v[-4:]}" if len(v) > 8 else "•••"
        else:
            out[k] = v
    return out


class EngineManager:
    KINDS = ("trend", "carry", "ml")

    def __init__(self, logbuf: LogBuffer):
        self.logbuf = logbuf
        self.threads: dict[str, threading.Thread] = {}
        self.events: dict[str, threading.Event] = {}
        self.objects: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self.started_at: dict[str, str] = {}
        self.session_ids: dict[str, str] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- settings
    @staticmethod
    def settings() -> Settings:
        return settings_from(read_env())

    def running(self, kind: str) -> bool:
        t = self.threads.get(kind)
        return bool(t and t.is_alive())

    # ---------------------------------------------------------------- sessions
    @staticmethod
    def _sessions() -> list[dict]:
        if SESSIONS_FILE.exists():
            try:
                return json.loads(SESSIONS_FILE.read_text())
            except Exception:
                return []
        return []

    @staticmethod
    def _save_sessions(rows: list[dict]) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps(rows[-500:], indent=2))

    def _session_update(self, sid: str, **kw) -> None:
        rows = self._sessions()
        for r in rows:
            if r["id"] == sid:
                r.update(kw)
        self._save_sessions(rows)

    # ---------------------------------------------------------------- control
    def start(self, kind: str, live_ack: bool = False) -> tuple[bool, str]:
        if kind not in self.KINDS:
            return False, "bilinmeyen motor"
        with self._lock:
            if self.running(kind):
                return False, "zaten çalışıyor"
            try:
                s = self.settings()
            except (ConfigError, ValueError) as e:
                return False, f"Ayar hatası: {e}"
            if s.mode == "live" and kind != "ml":   # the ML agent is paper-only, never touches an account
                if s.live_confirm != LIVE_CONFIRM_PHRASE:
                    return False, "Gerçek hesap modu onaylanmamış (Hesaplar sayfası)."
                if not live_ack:
                    return False, "Gerçek parayla başlatmak için onay kutusunu işaretle."
            if kind == "ml" and (read_env().get("ML_AGENT_ACCOUNT") or "paper") == "demo":
                from ..mlagent import demo_settings
                try:
                    demo_settings(read_env())
                except (ConfigError, ValueError):
                    return False, "Demo hesapta çalışması için Hesaplar sayfasında Binance Demo API anahtarlarını gir."
            if kind == "carry" and s.carry_capital <= 0:
                return False, "Carry için Ayarlar → Funding carry → sermaye gir."
            other = "trend" if kind == "carry" else "carry"
            if kind != "ml" and s.market == "futures" and self.running(other) and s.mode != "paper":
                return False, ("Futures trend botu ile carry aynı hesapta çalışamaz (pozisyonlar netleşir). "
                               "Carry için ayrı bir alt hesap kullan.")
            sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{kind}"
            ev = threading.Event()
            rows = self._sessions()
            rows.append({"id": sid, "engine": kind,
                         "mode": (read_env().get("ML_AGENT_ACCOUNT") or "paper") if kind == "ml" else s.mode,
                         "market": "spot" if kind == "ml" else s.market, "profile": s.profile,
                         "strategy": s.strategy, "symbols": s.symbols, "started": now_iso(), "ended": None,
                         "end_state": None, "start_equity": None, "end_equity": None,
                         "settings": redacted_settings(read_env())})
            self._save_sessions(rows)
            self.events[kind] = ev
            self.errors.pop(kind, None)
            self.started_at[kind] = now_iso()
            self.session_ids[kind] = sid
            self.logbuf.session = sid
            t = threading.Thread(target=self._run, args=(kind, s, ev, sid), name=f"engine-{kind}", daemon=True)
            self.threads[kind] = t
            t.start()
            return True, sid

    def _run(self, kind: str, s: Settings, ev: threading.Event, sid: str) -> None:
        obj = None
        try:
            if kind == "trend":
                from ..live import Bot
                obj = Bot(s)
            elif kind == "ml":
                from ..mlagent import MLAgent, demo_settings
                env = read_env()
                acc = env.get("ML_AGENT_ACCOUNT") or "paper"
                budget = float(env.get("ML_AGENT_BUDGET") or 1000)
                obj = MLAgent(demo_settings(env) if acc == "demo" else s, account=acc, budget=budget)
            else:
                from ..carry import CarryEngine
                obj = CarryEngine(s)
            self.objects[kind] = obj
            log.info("%s motoru başladı (oturum %s, mod=%s, piyasa=%s)", kind, sid,
                     getattr(obj, "account", s.mode), getattr(getattr(obj, "s", s), "market", s.market))
            obj.run(stop_event=ev)
        except Exception as e:  # construction errors (bad keys, no network, ...)
            log.exception("%s motoru hata ile durdu", kind)
            self.errors[kind] = f"{type(e).__name__}: {e}"
        finally:
            st = getattr(obj, "status", {}) if obj else {}
            if st.get("error") and st.get("state") == "error":
                self.errors[kind] = st["error"]
            self._session_update(sid, ended=now_iso(), end_state=st.get("state") or "error",
                                 end_equity=st.get("equity"), error=self.errors.get(kind))
            log.info("%s motoru durdu (%s)", kind, st.get("state") or "hata")

    def stop(self, kind: str, flatten: bool = False, wait: float = 20.0) -> tuple[bool, str]:
        ev = self.events.get(kind)
        if not ev or not self.running(kind):
            return False, "çalışmıyor"
        ev.set()
        t = self.threads[kind]
        t.join(wait)
        msg = "durduruldu" if not t.is_alive() else "durduruluyor (mevcut adım bitince)"
        if flatten:
            ok, m2 = self.flatten(kind)
            msg += f"; {m2}"
        return True, msg

    def flatten(self, kind: str) -> tuple[bool, str]:
        if self.running(kind):
            return False, "önce motoru durdur"
        try:
            s = self.settings()
            if kind == "trend":
                from ..live import Bot
                Bot(s).flatten("app_flatten")
            elif kind == "ml":
                from ..mlagent import MLAgent, demo_settings
                env = read_env()
                acc = env.get("ML_AGENT_ACCOUNT") or "paper"
                MLAgent(demo_settings(env) if acc == "demo" else s, account=acc).flatten()
            else:
                from ..carry import CarryEngine
                CarryEngine(s).flatten()
            log.info("%s: tüm bot pozisyonları kapatıldı", kind)
            return True, "pozisyonlar kapatıldı"
        except Exception as e:
            log.exception("flatten failed")
            return False, f"kapatılamadı: {e}"

    def stop_all(self) -> None:
        for k in self.KINDS:
            if self.running(k):
                self.stop(k, wait=10)

    def status(self) -> dict:
        out = {}
        for k in self.KINDS:
            obj = self.objects.get(k)
            st = dict(getattr(obj, "status", {}) or {}) if obj and self.running(k) else {}
            out[k] = {"running": self.running(k), "session": self.session_ids.get(k) if self.running(k) else None,
                      "started": self.started_at.get(k) if self.running(k) else None,
                      "error": self.errors.get(k) or st.get("error"), **{f"st_{a}": b for a, b in st.items()}}
        return out


def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()
