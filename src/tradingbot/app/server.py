"""Local HTTP server for the desktop app. Listens on 127.0.0.1 only and requires a per-launch token."""
from __future__ import annotations

import json
import logging
import mimetypes
import platform
import secrets
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ccxt

from .. import __version__
from ..config import LIVE_CONFIRM_PHRASE, PROFILES, ConfigError, Settings, settings_from
from ..envfile import ENV_PATH, read_env, write_env
from .engine import STATE_DIR, EngineManager, LogBuffer
from .export import EXPORT_DIR, build_export
from .livefeed import LiveFeed
from .schema import ALL_COINS, FIELDS, GROUPS, SECRET_KEYS

log = logging.getLogger("tradingbot.app")
STATIC = Path(__file__).parent / "static"
LIVE_TYPED_PHRASE = "GERÇEK PARA"


# ------------------------------------------------------------------------------------------ helpers
def _friendly_error(e: Exception) -> str:
    t = str(e)
    if isinstance(e, (ccxt.AuthenticationError, ccxt.PermissionDenied)) or "-2015" in t or "-2014" in t:
        return ("Binance anahtarı reddetti. Kontrol et: anahtar/secret doğru mu kopyalandı, doğru hesap mı "
                "(demo anahtarı sadece demo'da çalışır), IP kısıtlaması bu bilgisayarın IP'sine izin veriyor mu, "
                "'Enable Reading' ve işlem izni açık mı.")
    if isinstance(e, ccxt.NetworkError):
        return "Binance'e bağlanılamadı (internet bağlantısı veya Binance erişimi)."
    return f"{type(e).__name__}: {t[:300]}"


def _masked_values(env: dict[str, str]) -> dict:
    out = {}
    for f in FIELDS:
        k = f["key"]
        v = env.get(k, "")
        if k in SECRET_KEYS:
            out[k] = {"set": bool(v), "hint": (f"•••{v[-4:]}" if len(v) > 8 else ("•••" if v else ""))}
        else:
            out[k] = v
    return out


def test_connection(account: str, market: str, key: str, secret: str) -> dict:
    from ..exchange import make_exchange
    if not key or not secret:
        return {"ok": False, "error": "API Key ve Secret gerekli."}
    s = Settings(mode=account, market=market, api_key=key, api_secret=secret)
    try:
        ex = make_exchange(s)
        bal = ex.fetch_balance()
    except Exception as e:
        return {"ok": False, "error": _friendly_error(e)}
    total = bal.get("total", {}) or {}
    assets = sorted(((k, float(v)) for k, v in total.items() if v and float(v) > 0), key=lambda x: -x[1])[:8]
    res = {"ok": True, "usdt": float(total.get("USDT", 0) or 0), "assets": assets, "warnings": []}
    if account == "live":
        try:
            spot = make_exchange(Settings(mode="live", market="spot", api_key=key, api_secret=secret))
            r = spot.sapi_get_account_apirestrictions()
            if str(r.get("enableWithdrawals")).lower() == "true":
                res["warnings"].append("⚠️ Bu anahtarda PARA ÇEKME izni AÇIK. Binance'te kapat, bot buna ihtiyaç duymaz.")
            if str(r.get("ipRestrict")).lower() != "true":
                res["warnings"].append("⚠️ IP kısıtlaması kapalı. Binance'te anahtarı kendi IP adresine kısıtla.")
            if market == "futures" and str(r.get("enableFutures")).lower() != "true":
                res["warnings"].append("Futures izni kapalı: futures modunda işlem yapamaz.")
            if str(r.get("enableSpotAndMarginTrading")).lower() != "true":
                res["warnings"].append("Spot işlem izni kapalı: bot emir veremez.")
        except Exception:
            res["warnings"].append("Anahtar izinleri okunamadı; Binance'te 'para çekme' izninin kapalı olduğundan emin ol.")
    return res


def market_scan(s: Settings) -> list[dict]:
    from ..exchange import fetch_recent_closed, make_exchange
    from ..indicators import atr
    from ..strategies import btc_regime, make_target
    ex = make_exchange(s, authenticated=False)
    btc = fetch_recent_closed(ex, s.btc_symbol, s.timeframe, 1000)
    regime = "yükseliş" if btc_regime(btc).iloc[-1] > 0 else "düşüş"
    rows = []
    for sym in s.symbols:
        try:
            df = fetch_recent_closed(ex, sym, s.timeframe, 1000)
            t = int(make_target(s.strategy, df, s.allow_short, btc if s.btc_filter else None).iloc[-1])
            c, a = float(df["close"].iloc[-1]), float(atr(df).iloc[-1])
            rows.append({"symbol": sym, "price": c, "signal": {1: "LONG", -1: "SHORT"}.get(t, "BEKLE"),
                         "stop_pct": round(-s.atr_stop_mult * a / c * 100, 2), "bar": str(df.index[-1]),
                         "btc_regime": regime})
        except Exception as e:
            rows.append({"symbol": sym, "error": _friendly_error(e)})
    return rows


def _read_csv(path):
    """Tolerant CSV read: the bot may be appending to the file at this very moment."""
    import pandas as pd
    try:
        return pd.read_csv(path, on_bad_lines="skip")
    except Exception:
        return pd.DataFrame()


def _read_json(path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def dashboard(s: Settings, eng: EngineManager) -> dict:
    import pandas as pd
    tag = f"{s.mode}_{s.market}"
    out: dict = {"tag": tag}
    bot_f, risk_f = STATE_DIR / f"bot_{tag}.json", STATE_DIR / f"risk_{tag}.json"
    eq_f, j_f = STATE_DIR / f"equity_{tag}.csv", STATE_DIR / f"journal_{tag}.csv"
    bot = _read_json(bot_f) if bot_f.exists() else {}
    risk = _read_json(risk_f) if risk_f.exists() else {}
    out["open"] = [{"symbol": k, **v} for k, v in (bot.get("trades") or {}).items()]
    out["last_bar"] = bot.get("last_bar", {})
    out["risk"] = {"halted": risk.get("halted", False), "reason": risk.get("halt_reason", ""),
                   "peak": risk.get("peak_equity"), "day_start": risk.get("day_start_equity")}
    series = []
    e = _read_csv(eq_f) if eq_f.exists() else pd.DataFrame()
    if len(e) and {"time", "equity"} <= set(e.columns):
        e = e.tail(3000)
        series = [[str(t), float(v)] for t, v in zip(e["time"], e["equity"])]
    live_eq = (eng.status().get("trend") or {}).get("st_equity")
    if live_eq is not None:
        series.append([pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"), float(live_eq)])
    out["equity_series"] = series
    trades = []
    kpi = {"closes": 0, "wins": 0, "pnl": 0.0}
    j = _read_csv(j_f) if j_f.exists() else pd.DataFrame()
    if len(j) and "action" in j.columns:
        closes = j[j["action"] == "close"]
        pnl = closes["pnl_est"].astype(float)
        kpi = {"closes": int(len(closes)), "wins": int((pnl > 0).sum()), "pnl": float(pnl.sum())}
        trades = j.tail(200).iloc[::-1].fillna("").to_dict("records")
    out["trades"] = trades
    if series:
        vals = [v for _, v in series]
        peak, dd = vals[0], 0.0
        for v in vals:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1 if peak else 0)
        kpi.update(equity=vals[-1], start=vals[0], ret=(vals[-1] / vals[0] - 1) * 100 if vals[0] else 0,
                   max_dd=dd * 100)
    out["kpi"] = kpi
    carry_f = STATE_DIR / f"carry_{s.mode}.json"
    out["carry"] = _read_json(carry_f) if carry_f.exists() else {}
    sessions = eng._sessions()
    out["sessions"] = [{k: v for k, v in r.items() if k != "settings"} for r in sessions[-30:]][::-1]
    return out


def _reveal(path: Path) -> None:
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif platform.system() == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception:
        log.warning("could not open file manager for %s", path)


class ValidationJob:
    """Runs `validation.run_validation` in a background thread (takes ~30-90 s, first run downloads data)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.progress = ""
        self.error: str | None = None
        self.started: float | None = None
        self.market: str | None = None

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, s: Settings, runner=None) -> bool:
        with self._lock:
            if self.running():
                return False
            self.error, self.progress, self.started, self.market = None, "veri yükleniyor", time.time(), s.market
            self.thread = threading.Thread(target=self._run, args=(s, runner), name="validation", daemon=True)
            self.thread.start()
            return True

    def _run(self, s: Settings, runner) -> None:
        from .. import validation
        try:
            log.info("Doğrulama testleri başladı (%s, %d coin)", s.market, len(s.symbols))
            if runner is None:
                full, btc = validation.load_for(s)
                if not full:
                    raise RuntimeError("Geçmiş veri yüklenemedi (internet bağlantısını kontrol et).")
                res = validation.run_validation(s, full, btc, progress=self._step)
            else:
                res = runner(s, self._step)
            self._step("rapor yazılıyor")
            validation.save(res, STATE_DIR)
            h = res["periods"]["holdout"]
            mc = res.get("mc_holdout") or res["mc_all"]
            log.info("Doğrulama bitti: saklı dönem yıllık %%%.1f, 12 ay zarar olasılığı %%%.0f",
                     h["cagr_%"], mc["prob_loss"] * 100)
        except Exception as e:
            self.error = _friendly_error(e) if not isinstance(e, RuntimeError) else str(e)
            log.error("Doğrulama başarısız: %s", self.error)
        finally:
            self.progress = ""

    def _step(self, msg: str) -> None:
        self.progress = msg
        log.info("Doğrulama: %s", msg)

    def status(self) -> dict:
        return {"running": self.running(), "progress": self.progress, "error": self.error,
                "elapsed": round(time.time() - self.started) if self.started and self.running() else None}


class LearnJob(ValidationJob):
    """(Re)trains the learning trade filter on all history up to now, in the background."""

    def _run(self, s: Settings, runner) -> None:
        from .. import learner, validation
        try:
            log.info("Öğrenen model eğitiliyor (%s)", s.market)
            if runner is None:
                full, btc = validation.load_for(s)
                if not full:
                    raise RuntimeError("Geçmiş veri yüklenemedi (internet bağlantısını kontrol et).")
            else:
                full, btc = runner(s)
            learner.build(s, full, btc, progress=self._step)
        except Exception as e:
            self.error = _friendly_error(e) if not isinstance(e, RuntimeError) else str(e)
            log.error("Öğrenen model eğitilemedi: %s", self.error)
        finally:
            self.progress = ""


class MLTrainJob(ValidationJob):
    """Downloads hourly history since 2017 (first time only) and trains the paper ML agent."""

    def _run(self, s: Settings, runner) -> None:
        from .. import mlagent
        try:
            log.info("ML ajan eğitimi başladı")
            data, btc = (runner(s) if runner else mlagent.load_training_data(s, progress=self._step))
            if not data:
                raise RuntimeError("Saatlik veri yüklenemedi (internet bağlantısını kontrol et).")
            mlagent.train(data, btc, progress=self._step)
        except ImportError:
            self.error = "scikit-learn kurulu değil. Uygulamayı kapatıp açınca kendisi kurar."
            log.error("ML ajan: %s", self.error)
        except Exception as e:
            self.error = _friendly_error(e) if not isinstance(e, RuntimeError) else str(e)
            log.error("ML ajan eğitilemedi: %s", self.error)
        finally:
            self.progress = ""


def ml_summary(engines, env: dict) -> dict:
    from .. import mlagent
    acc = env.get("ML_AGENT_ACCOUNT") or "paper"
    budget = float(env.get("ML_AGENT_BUDGET") or mlagent.START_CASH)
    mp, metap, sp, jp, ep = mlagent._paths(acc)
    meta = mlagent.load_meta()
    out = {"meta": meta, "state": _read_json(sp) if sp.exists() else None, "account": acc,
           "demo_keys": bool(env.get("BINANCE_DEMO_API_KEY") and env.get("BINANCE_DEMO_API_SECRET")),
           "threshold": mlagent.THRESHOLD, "horizon": mlagent.HORIZON, "start_cash": budget}
    j = _read_csv(jp) if jp.exists() else None
    exp = ((meta or {}).get("holdout_check") or {}).get("win_rate")
    out["learning"] = mlagent.learning_curve(j, exp)
    out["trades"] = j.tail(200).iloc[::-1].fillna("").to_dict("records") if j is not None and len(j) else []
    if j is not None and len(j) and "action" in j.columns:
        c = j[j["action"] == "close"]
        out["kpi"] = {"closes": int(len(c)), "wins": int((c["pnl_est"].astype(float) > 0).sum()),
                      "pnl": float(c["pnl_est"].astype(float).sum())}
    e = _read_csv(ep) if ep.exists() else None
    out["equity_series"] = [[str(t), float(v)] for t, v in zip(e["time"], e["equity"])] if e is not None and len(e) else []
    st = engines.status().get("ml") or {}
    if st.get("st_equity") is not None:
        out["equity_series"].append([datetime.now(timezone.utc).isoformat(timespec="seconds"), float(st["st_equity"])])
    return out


def _cached_history(s: Settings) -> tuple[dict, object]:
    """Candles from the local cache only (no network): for scoring the bot's own trades."""
    import pandas as pd
    from ..data import _cache_path
    out = {}
    for sym in list(s.symbols) + [s.btc_symbol]:
        f = _cache_path(s.market, sym, s.timeframe)
        if f.exists():
            d = pd.read_csv(f, index_col=0, parse_dates=True)
            if d.index.tz is None:
                d.index = d.index.tz_localize("UTC")
            out[sym] = d
    btc = out.get(s.btc_symbol) if s.btc_filter else None
    return {k: v for k, v in out.items() if k in s.symbols}, btc


def learner_summary(env: dict) -> dict:
    from .. import learner
    s = settings_from({**env, "MODE": env.get("MODE", "paper") or "paper"})
    m = learner.load_model(s.market)
    out = {"market": s.market, "mode": s.learner_mode, "model": None, "shadow": []}
    if not m:
        return out
    out["model"] = {k: m.get(k) for k in ("trained_at", "trained_until", "trades", "win_rate", "approved",
                                          "approval_reason", "walk_forward", "weights", "threshold", "symbols")}
    out["active"] = bool(m.get("approved") and s.learner_mode == "filter")
    j = STATE_DIR / f"journal_{s.mode}_{s.market}.csv"
    if j.exists():
        full, btc = _cached_history(s)
        try:
            out["shadow"] = learner.shadow_report(s, full, btc, _read_csv(j))[-100:][::-1]
        except Exception as e:
            out["shadow_error"] = str(e)[:200]
    return out


def validation_summary(env: dict) -> dict:
    """Saved validation for the current market + fresh comparison with what the bot actually did."""
    from .. import validation
    market = env.get("MARKET", "spot") or "spot"
    res = validation.load_saved(market)
    if not res:
        return {"result": None, "market": market}
    try:
        stale = validation.stale_reason(res, settings_from({**env, "MODE": "paper"}))
    except (ConfigError, ValueError):
        stale = None
    keep = ("generated", "settings", "periods", "verdict", "risk_curve", "pbo", "dsr", "psr", "psr_holdout",
            "cost_stress", "yearly", "monthly", "streaks")
    out = {k: res.get(k) for k in keep}
    for k in ("mc_all", "mc_holdout"):
        mc = res.get(k)
        out[k] = None if not mc else {x: mc[x] for x in ("median", "p5", "p25", "p75", "p95", "prob_loss",
                                                         "prob_dd_gt_20", "prob_dd_gt_30", "median_max_dd")}
    return {"result": out, "market": market, "stale": stale, "live": validation.live_tracking(res, STATE_DIR)}


# ------------------------------------------------------------------------------------------ server
class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.logbuf = LogBuffer()
        root = logging.getLogger()
        root.addHandler(self.logbuf)          # Output panel sees every log line of the bots
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        self.engines = EngineManager(self.logbuf)
        self.validation = ValidationJob()
        self.learn = LearnJob()
        self.mltrain = MLTrainJob()
        self.feed = LiveFeed()
        self.port = 0

    def auto_jobs(self) -> None:
        """Weekly retraining of the learning model (started by the desktop app, not by tests)."""
        from .. import learner

        def loop():
            while True:
                try:
                    s = settings_from({**read_env(), "MODE": "paper"})
                    if s.learner_mode != "off" and learner.needs_retrain(s.market) and not self.learn.running():
                        self.learn.start(s)
                    from .. import mlagent
                    if mlagent.load_meta() and mlagent.needs_retrain() and not self.mltrain.running():
                        self.mltrain.start(s)
                except Exception:
                    log.exception("auto retrain check failed")
                time.sleep(3600)
        threading.Thread(target=loop, name="learner-auto", daemon=True).start()

    # every handler returns (status, payload-dict)
    def api(self, method: str, path: str, q: dict, body: dict) -> tuple[int, dict]:
        env = read_env()
        if path == "/api/bootstrap":
            try:
                s = settings_from(env)
                cfg_err = None
            except (ConfigError, ValueError) as e:
                s, cfg_err = None, str(e)
            return 200, {"version": __version__, "configured": env.get("APP_SETUP_DONE") == "1",
                         "env_exists": ENV_PATH.exists(), "fields": FIELDS, "groups": GROUPS,
                         "values": _masked_values(env), "profiles": PROFILES, "coins": ALL_COINS,
                         "config_error": cfg_err, "mode": env.get("MODE", "paper") or "paper",
                         "market": env.get("MARKET", "spot") or "spot",
                         "live_confirmed": env.get("LIVE_TRADING_CONFIRM") == LIVE_CONFIRM_PHRASE,
                         "effective": None if s is None else {
                             "risk_per_trade": s.risk_per_trade, "atr_stop_mult": s.atr_stop_mult,
                             "max_open_positions": s.max_open_positions, "max_daily_loss": s.max_daily_loss,
                             "max_drawdown": s.max_drawdown, "fee": s.fee, "symbols": s.symbols},
                         "platform": platform.system()}
        if path == "/api/settings" and method == "POST":
            if any(self.engines.running(k) for k in EngineManager.KINDS):
                return 409, {"error": "Ayarları değiştirmeden önce botu durdur."}
            updates = {}
            for k, v in (body.get("values") or {}).items():
                if k not in {f["key"] for f in FIELDS} | {"APP_SETUP_DONE"}:
                    continue
                if k == "MODE":
                    continue  # mode changes go through /api/account/switch
                if k in SECRET_KEYS and (v is None or str(v).startswith("•••")):
                    continue  # unchanged secret
                updates[k] = "" if v is None else str(v).strip()
            merged = {**env, **updates}
            try:
                settings_from({**merged, "MODE": "paper"})  # validate everything except account keys
            except (ConfigError, ValueError) as e:
                return 400, {"error": str(e)}
            write_env(updates)
            log.info("Ayarlar kaydedildi: %s", ", ".join(sorted(updates)) or "değişiklik yok")
            return 200, {"ok": True}
        if path == "/api/account/switch" and method == "POST":
            if any(self.engines.running(k) for k in EngineManager.KINDS):
                return 409, {"error": "Hesap değiştirmeden önce botu durdur."}
            mode = body.get("mode")
            if mode not in ("paper", "demo", "live"):
                return 400, {"error": "geçersiz mod"}
            upd = {"MODE": mode}
            if mode == "live":
                if (body.get("phrase") or "").strip().upper() != LIVE_TYPED_PHRASE:
                    return 400, {"error": f"Onay için tam olarak '{LIVE_TYPED_PHRASE}' yaz."}
                upd["LIVE_TRADING_CONFIRM"] = LIVE_CONFIRM_PHRASE
            else:
                upd["LIVE_TRADING_CONFIRM"] = ""
            try:
                settings_from({**env, **upd})
            except (ConfigError, ValueError) as e:
                return 400, {"error": str(e)}
            write_env(upd)
            log.info("Hesap modu: %s", mode.upper())
            return 200, {"ok": True, "mode": mode}
        if path == "/api/test-connection" and method == "POST":
            acc = body.get("account", "demo")
            slot = "BINANCE_DEMO_" if acc == "demo" else "BINANCE_LIVE_"
            key = body.get("api_key") or env.get(slot + "API_KEY") or env.get("BINANCE_API_KEY", "")
            sec = body.get("api_secret") or env.get(slot + "API_SECRET") or env.get("BINANCE_API_SECRET", "")
            if str(key).startswith("•••"):
                key = env.get(slot + "API_KEY", "")
            if str(sec).startswith("•••"):
                sec = env.get(slot + "API_SECRET", "")
            return 200, test_connection(acc, body.get("market") or env.get("MARKET", "spot") or "spot", key, sec)
        if path == "/api/telegram/test" and method == "POST":
            from ..notify import Notifier
            tok = body.get("token") or env.get("TELEGRAM_BOT_TOKEN", "")
            if str(tok).startswith("•••"):
                tok = env.get("TELEGRAM_BOT_TOKEN", "")
            n = Notifier(tok, body.get("chat_id") or env.get("TELEGRAM_CHAT_ID", ""), "[TradingBot]")
            if not n.enabled:
                return 200, {"ok": False, "error": "Token ve chat ID gerekli."}
            return 200, {"ok": n.send("Test mesajı: bildirimler çalışıyor ✅", block=True)}
        if path == "/api/telegram/find-chat" and method == "POST":
            tok = body.get("token") or env.get("TELEGRAM_BOT_TOKEN", "")
            if str(tok).startswith("•••"):
                tok = env.get("TELEGRAM_BOT_TOKEN", "")
            try:
                with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10) as r:
                    data = json.loads(r.read())
                chats = {}
                for u in data.get("result", []):
                    c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
                    if c.get("id"):
                        chats[str(c["id"])] = c.get("username") or c.get("title") or c.get("first_name") or ""
                if not chats:
                    return 200, {"ok": False, "error": "Önce Telegram'da botuna bir mesaj gönder, sonra tekrar dene."}
                return 200, {"ok": True, "chats": [[k, v] for k, v in chats.items()]}
            except Exception as e:
                return 200, {"ok": False, "error": f"Token geçersiz veya Telegram'a ulaşılamadı ({type(e).__name__})."}
        if path == "/api/engine/start" and method == "POST":
            ok, msg = self.engines.start(body.get("kind", "trend"), live_ack=bool(body.get("live_ack")))
            return (200 if ok else 400), ({"ok": True, "session": msg} if ok else {"error": msg})
        if path == "/api/engine/stop" and method == "POST":
            ok, msg = self.engines.stop(body.get("kind", "trend"), flatten=bool(body.get("flatten")))
            return (200 if ok else 400), ({"ok": True, "message": msg} if ok else {"error": msg})
        if path == "/api/engine/flatten" and method == "POST":
            ok, msg = self.engines.flatten(body.get("kind", "trend"))
            return (200 if ok else 400), ({"ok": True, "message": msg} if ok else {"error": msg})
        if path == "/api/status":
            return 200, {"engines": self.engines.status(), "mode": env.get("MODE", "paper") or "paper",
                         "market": env.get("MARKET", "spot") or "spot"}
        if path == "/api/dashboard":
            try:
                s = settings_from(env)
            except (ConfigError, ValueError) as e:
                return 200, {"config_error": str(e)}
            return 200, dashboard(s, self.engines)
        if path == "/api/logs":
            return 200, {"records": self.logbuf.since(int(q.get("after", ["0"])[0] or 0))}
        if path == "/api/scan" and method == "POST":
            try:
                return 200, {"rows": market_scan(settings_from({**env, "MODE": "paper"}))}
            except Exception as e:
                return 200, {"error": _friendly_error(e)}
        if path == "/api/export" and method == "POST":
            p = build_export(list(self.logbuf.records))
            log.info("Veriler dışa aktarıldı: %s", p)
            return 200, {"ok": True, "path": str(p), "name": p.name}
        if path == "/api/reveal" and method == "POST":
            name = Path(body.get("name", "")).name
            target = EXPORT_DIR / name if name else EXPORT_DIR
            _reveal(target if target.exists() else EXPORT_DIR)
            return 200, {"ok": True}
        if path == "/api/report" and method == "POST":
            from ..report import build_report
            p = build_report()
            import webbrowser
            webbrowser.open(p.as_uri())
            return 200, {"ok": True, "path": str(p)}
        if path in ("/api/live/overview", "/api/live/chart"):
            try:
                s = settings_from(env)
            except (ConfigError, ValueError) as e:
                return 200, {"error": str(e)}
            trades = _read_json(STATE_DIR / f"bot_{s.mode}_{s.market}.json").get("trades") or {}
            try:
                if path == "/api/live/overview":
                    return 200, {**self.feed.overview(s, trades), "mode": s.mode, "market": s.market}
                sym = q.get("symbol", [s.symbols[0]])[0]
                if sym not in s.symbols and sym != s.btc_symbol:
                    return 400, {"error": "bilinmeyen coin"}
                out = self.feed.chart(s, sym, q.get("tf", [s.timeframe])[0])
                out["position"] = trades.get(sym)
                out["bot_timeframe"] = s.timeframe
                return 200, out
            except Exception as e:
                return 200, {"error": _friendly_error(e)}
        if path == "/api/ml":
            return 200, {**ml_summary(self.engines, env), "job": self.mltrain.status()}
        if path == "/api/ml/account" and method == "POST":
            if self.engines.running("ml"):
                return 409, {"error": "Önce ML ajanı durdur."}
            acc = body.get("account")
            if acc not in ("paper", "demo"):
                return 400, {"error": "Hesap paper ya da demo olabilir (gerçek hesap bu ajana kapalı)."}
            try:
                budget = float(body.get("budget") or 1000)
            except (TypeError, ValueError):
                return 400, {"error": "Bütçe sayı olmalı."}
            if not 50 <= budget <= 1_000_000:
                return 400, {"error": "Bütçe 50 ile 1.000.000 USDT arasında olmalı."}
            write_env({"ML_AGENT_ACCOUNT": acc, "ML_AGENT_BUDGET": f"{budget:g}"})
            log.info("ML ajan hesabı: %s, bütçe %g USDT", acc.upper(), budget)
            return 200, {"ok": True}
        if path == "/api/ml/train" and method == "POST":
            try:
                s = settings_from({**env, "MODE": "paper"})
            except (ConfigError, ValueError) as e:
                return 400, {"error": str(e)}
            if not self.mltrain.start(s):
                return 409, {"error": "Eğitim zaten çalışıyor."}
            return 200, {"ok": True}
        if path == "/api/ml/reset" and method == "POST":
            if self.engines.running("ml"):
                return 409, {"error": "Önce ML ajanı durdur."}
            from .. import mlagent
            acc = env.get("ML_AGENT_ACCOUNT") or "paper"
            st = _read_json(mlagent._paths(acc)[2])
            if acc == "demo" and st.get("positions"):
                return 409, {"error": "Demo hesapta açık pozisyon var. Önce 'Pozisyonları kapat'."}
            for f in mlagent._paths(acc)[2:]:
                if f.exists():
                    f.unlink()
            log.info("ML ajan paper hesabı sıfırlandı")
            return 200, {"ok": True}
        if path == "/api/learner":
            try:
                return 200, {**learner_summary(env), "job": self.learn.status()}
            except (ConfigError, ValueError) as e:
                return 200, {"error": str(e), "job": self.learn.status()}
        if path == "/api/learner/train" and method == "POST":
            try:
                s = settings_from({**env, "MODE": "paper"})
            except (ConfigError, ValueError) as e:
                return 400, {"error": str(e)}
            if not self.learn.start(s):
                return 409, {"error": "Eğitim zaten çalışıyor."}
            return 200, {"ok": True}
        if path == "/api/validation":
            return 200, {**validation_summary(env), "job": self.validation.status()}
        if path == "/api/validation/run" and method == "POST":
            try:
                s = settings_from({**env, "MODE": "paper"})
            except (ConfigError, ValueError) as e:
                return 400, {"error": str(e)}
            if not self.validation.start(s):
                return 409, {"error": "Testler zaten çalışıyor."}
            return 200, {"ok": True}
        if path == "/api/validation/open" and method == "POST":
            from .. import validation
            res = validation.load_saved(env.get("MARKET", "spot") or "spot")
            if not res:
                return 404, {"error": "Önce testleri çalıştır."}
            _, h = validation.save(res, STATE_DIR)   # refresh the live-tracking part
            import webbrowser
            webbrowser.open(h.as_uri())
            return 200, {"ok": True, "path": str(h)}
        if path == "/api/reset-halt" and method == "POST":
            s = settings_from(env)
            from ..risk import RiskManager
            rm = RiskManager(s.max_daily_loss, s.max_drawdown, s.max_open_positions,
                             STATE_DIR / f"risk_{s.mode}_{s.market}.json")
            rm.reset_halt()
            log.info("Kill switch sıfırlandı (%s/%s)", s.mode, s.market)
            return 200, {"ok": True}
        if path == "/api/paper-reset" and method == "POST":
            if self.engines.running("trend") or self.engines.running("carry"):
                return 409, {"error": "Önce botu durdur."}
            removed = []
            for pat in ("paper_*.json", "bot_paper_*.json", "risk_paper_*.json", "carry_paper.json",
                        "journal_paper_*.csv", "equity_paper_*.csv"):
                for f in STATE_DIR.glob(pat):
                    f.unlink()
                    removed.append(f.name)
            log.info("Paper hesabı sıfırlandı (%d dosya)", len(removed))
            return 200, {"ok": True, "removed": removed}
        if path == "/api/setup-done" and method == "POST":
            write_env({"APP_SETUP_DONE": "1"})
            return 200, {"ok": True}
        return 404, {"error": "not found"}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingBot"

        def log_message(self, fmt, *args):  # keep the console quiet
            return

        def _authorized(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return False  # DNS-rebinding protection
            if self.headers.get("X-Token") == app.token:
                return True
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return "tbtoken" in c and c["tbtoken"].value == app.token

        def _send(self, code: int, payload: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def _route(self, method: str):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                if q.get("t", [""])[0] != app.token and not self._authorized():
                    return self._send(403, b"Forbidden", "text/plain")
                html = (STATIC / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8", {
                    "Set-Cookie": f"tbtoken={app.token}; HttpOnly; SameSite=Strict; Path=/",
                    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            if u.path == "/download":
                name = Path(q.get("name", [""])[0]).name
                f = EXPORT_DIR / name
                if not name or not f.exists():
                    return self._json(404, {"error": "dosya yok"})
                return self._send(200, f.read_bytes(), "application/zip",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if u.path.startswith("/api/"):
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        try:
                            body = json.loads(self.rfile.read(n) or b"{}")
                        except json.JSONDecodeError:
                            return self._json(400, {"error": "bad json"})
                try:
                    code, payload = app.api(method, u.path, q, body)
                except Exception as e:
                    log.exception("API error %s", u.path)
                    code, payload = 500, {"error": _friendly_error(e)}
                return self._json(code, payload)
            f = (STATIC / u.path.lstrip("/")).resolve()
            if STATIC in f.parents and f.is_file():
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            return self._json(404, {"error": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def serve(app: App, port: int = 0) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd
