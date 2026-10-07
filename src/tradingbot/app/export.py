"""Builds a zip with everything needed to analyse a trading session later (secrets removed)."""
from __future__ import annotations

import io
import json
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import PROJECT_ROOT
from ..envfile import read_env
from .engine import STATE_DIR, redacted_settings
from .schema import SECRET_KEYS

EXPORT_DIR = PROJECT_ROOT / "exports"
LOG_DIR = PROJECT_ROOT / "logs"
MAX_LOG_BYTES = 15 * 1024 * 1024

README = """TradingBot veri dışa aktarımı
==============================
Bu zip, botun çalışma verilerini içerir. API anahtarları, secret'lar ve Telegram token'ı
ÇIKARILMIŞTIR (••• ile maskeli). Geliştirme için Claude'a olduğu gibi gönderebilirsin.

İçerik
- summary.json            : özet (oturumlar, işlem sayısı, PnL, özsermaye başı/sonu)
- sessions.json           : her Başlat/Durdur oturumu: zaman, mod, piyasa, ayarlar (maskeli)
- settings_redacted.json  : şu anki ayarlar (maskeli)
- environment.json        : uygulama/python/paket sürümleri, işletim sistemi, git commit
- state/journal_*.csv     : her açılış/kapanış: zaman, coin, yön, miktar, fiyat, stop, sebep, tahmini PnL
- state/equity_*.csv      : saatlik özsermaye
- state/*.json            : botun anlık durumu (açık pozisyonlar, risk/kill-switch durumu, carry)
- logs/*.log              : ayrıntılı çalışma kayıtları (son 15 MB)
- app_output.jsonl        : uygulamadaki Çıktı panelinin içeriği
- report.html             : görsel rapor (tarayıcıda aç)
- validation/*            : son doğrulama testleri (Monte Carlo, Deflated Sharpe, PBO, risk seviyeleri)
"""


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except Exception:
        return "unknown"


def _versions() -> dict:
    out = {}
    for mod in ("ccxt", "pandas", "numpy", "webview"):
        try:
            out[mod] = getattr(__import__(mod), "__version__", "?")
        except Exception:
            out[mod] = None
    return out


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "•••REDACTED•••")
    return text


def _summary() -> dict:
    import pandas as pd
    out = {"journals": {}}
    for j in sorted(STATE_DIR.glob("journal_*.csv")):
        try:
            df = pd.read_csv(j)
        except Exception:
            continue
        closes = df[df["action"] == "close"]
        pnl = closes["pnl_est"].astype(float) if len(closes) else pd.Series(dtype=float)
        out["journals"][j.stem.replace("journal_", "")] = {
            "opens": int((df["action"] == "open").sum()), "closes": int(len(closes)),
            "wins": int((pnl > 0).sum()), "pnl_est_total": round(float(pnl.sum()), 4),
            "first": str(df["time"].iloc[0]) if len(df) else None,
            "last": str(df["time"].iloc[-1]) if len(df) else None,
            "by_reason": closes["reason"].value_counts().to_dict() if len(closes) else {},
        }
    for e in sorted(STATE_DIR.glob("equity_*.csv")):
        try:
            df = pd.read_csv(e)
            v = df["equity"].astype(float)
            out.setdefault("equity", {})[e.stem.replace("equity_", "")] = {
                "first": float(v.iloc[0]), "last": float(v.iloc[-1]),
                "max_drawdown_%": round(float((v / v.cummax() - 1).min() * 100), 3), "points": int(len(v))}
        except Exception:
            continue
    return out


def build_export(output_records: list[dict] | None = None) -> Path:
    env = read_env()
    secrets = [v for k, v in env.items() if k in SECRET_KEYS and v]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"TradingBot_veri_{stamp}.zip"
    try:
        from ..report import build_report
        report = build_report()
    except Exception:
        report = None
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        z.writestr("settings_redacted.json", json.dumps(redacted_settings(env), indent=2, ensure_ascii=False))
        z.writestr("environment.json", json.dumps({
            "exported_at": stamp, "python": sys.version, "platform": platform.platform(),
            "machine": platform.machine(), "git_commit": _git_commit(), "packages": _versions()}, indent=2))
        z.writestr("summary.json", json.dumps(_summary(), indent=2, ensure_ascii=False, default=str))
        if STATE_DIR.exists():
            for f in sorted(STATE_DIR.iterdir()):
                if f.is_file() and f.suffix in (".csv", ".json"):
                    z.writestr(f"state/{f.name}", _scrub(f.read_text(errors="replace"), secrets))
        if LOG_DIR.exists():
            for f in sorted(LOG_DIR.glob("*.log")):
                data = f.read_bytes()[-MAX_LOG_BYTES:]
                z.writestr(f"logs/{f.name}", _scrub(data.decode("utf-8", "replace"), secrets))
        if output_records:
            buf = io.StringIO()
            for r in output_records:
                buf.write(json.dumps(r, ensure_ascii=False) + "\n")
            z.writestr("app_output.jsonl", _scrub(buf.getvalue(), secrets))
        if report and report.exists():
            z.writestr("report.html", report.read_text(encoding="utf-8"))
        from .. import validation
        for f in sorted(validation.OUT_DIR.glob("validation_*.*")):
            if f.suffix in (".json", ".html"):
                z.writestr(f"validation/{f.name}", f.read_text(encoding="utf-8", errors="replace"))
    return path
