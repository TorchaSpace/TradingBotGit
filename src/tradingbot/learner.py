"""Learning trade filter: estimates, at entry time, how likely a new signal is to end as a winner.

How it learns
- Every (re)training replays the bot's own strategy on all history up to today (including the
  weeks the bot has been running) and learns from every resulting trade: what the chart looked
  like at entry (trend strength, distance to EMA200, volatility, momentum, BTC regime) -> win/loss.
- The app retrains automatically once a week, so new market data keeps flowing in.

How it is kept honest
- Walk-forward test: for each year, train only on trades that ended before that year, then score
  that year's trades. "Filter effect" = total P/L of the trades it would have skipped.
- The model may only be used to SKIP the weakest signals (never to add trades or raise risk),
  and only when it is APPROVED: out-of-sample AUC >= 0.55 and skipping helped in the holdout years.
- Modes (LEARNER_MODE): off | shadow (default: scores every trade, changes nothing) | filter.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT, Settings
from .indicators import atr, ema, rsi

log = logging.getLogger(__name__)
STATE_DIR = PROJECT_ROOT / "state"
FEATURES = ["trend_gap", "vs_ema200", "atr_pct", "rsi", "ret_30", "ret_120", "vol_ratio", "btc_vs_ema200"]
DIRECTIONAL = {"trend_gap", "vs_ema200", "ret_30", "ret_120", "btc_vs_ema200"}  # flipped for shorts
SKIP_QUANTILE = 0.30      # in filter mode: skip signals scoring in the weakest 30% of training trades
MIN_AUC = 0.55
RETRAIN_DAYS = 7


def model_path(market: str) -> Path:
    return STATE_DIR / f"learner_{market}.json"


# ------------------------------------------------------------------------------------------ features
def feature_frame(df: pd.DataFrame, btc: pd.DataFrame | None) -> pd.DataFrame:
    """One row per bar, computed only from data up to that bar's close."""
    c = df["close"]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    a = atr(df)
    f = pd.DataFrame({
        "trend_gap": e20 / e50 - 1, "vs_ema200": c / e200 - 1, "atr_pct": a / c, "rsi": rsi(c, 14) / 100,
        "ret_30": c.pct_change(30), "ret_120": c.pct_change(120), "vol_ratio": a / a.rolling(100).mean(),
    }, index=df.index)
    if btc is not None and len(btc):
        b = btc["close"]
        f["btc_vs_ema200"] = (b / ema(b, 200) - 1).reindex(df.index, method="ffill")
    else:
        f["btc_vs_ema200"] = 0.0
    return f


def entry_features(f: pd.DataFrame, entry_time, direction: int) -> np.ndarray | None:
    """Features of the last bar that had CLOSED before the entry (no look-ahead)."""
    rows = f[f.index < entry_time]
    if not len(rows):
        return None
    x = rows.iloc[-1][FEATURES].to_numpy(float).copy()
    if np.isnan(x).any():
        return None
    for i, k in enumerate(FEATURES):
        if k in DIRECTIONAL:
            x[i] *= direction
    return x


def trade_dataset(full: dict, btc: pd.DataFrame | None, trades: pd.DataFrame) -> pd.DataFrame:
    frames = {k: feature_frame(d, btc) for k, d in full.items()}
    rows = []
    for t in trades.itertuples():
        x = entry_features(frames[t.symbol], t.entry_time, int(t.direction))
        if x is not None:
            rows.append([t.symbol, t.entry_time, t.exit_time, int(t.direction), float(t.pnl), *x])
    return pd.DataFrame(rows, columns=["symbol", "entry_time", "exit_time", "direction", "pnl", *FEATURES])


# ------------------------------------------------------------------------------------------ model
def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50) -> dict:
    mu, sd = X.mean(0), X.std(0) + 1e-12
    Z = np.c_[np.ones(len(X)), (X - mu) / sd]
    w = np.zeros(Z.shape[1])
    reg = np.r_[0.0, np.full(Z.shape[1] - 1, l2)]
    for _ in range(iters):  # Newton-Raphson with ridge penalty
        p = 1 / (1 + np.exp(-Z @ w))
        g = Z.T @ (p - y) + reg * w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg + 1e-9)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist()}


def predict(m: dict, X: np.ndarray) -> np.ndarray:
    Z = (np.atleast_2d(X) - np.array(m["mu"])) / np.array(m["sd"])
    w = np.array(m["w"])
    return 1 / (1 + np.exp(-(w[0] + Z @ w[1:])))


def auc(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y).astype(bool)
    if y.all() or (~y).all():
        return float("nan")
    r = pd.Series(p).rank().to_numpy()
    return float((r[y].sum() - y.sum() * (y.sum() + 1) / 2) / (y.sum() * (~y).sum()))


def walk_forward(ds: pd.DataFrame, years: list[int]) -> list[dict]:
    out = []
    for yr in years:
        start = pd.Timestamp(f"{yr}-01-01", tz="UTC")
        tr = ds[ds["exit_time"] < start]
        te = ds[(ds["entry_time"] >= start) & (ds["entry_time"] < start + pd.DateOffset(years=1))]
        if len(tr) < 100 or len(te) < 20:
            continue
        m = fit_logistic(tr[FEATURES].to_numpy(), (tr["pnl"] > 0).to_numpy(float))
        thr = float(np.quantile(predict(m, tr[FEATURES].to_numpy()), SKIP_QUANTILE))
        p = predict(m, te[FEATURES].to_numpy())
        skip = p < thr
        out.append({"year": yr, "train_trades": int(len(tr)), "test_trades": int(len(te)),
                    "auc": auc((te["pnl"] > 0).to_numpy(), p), "skipped": int(skip.sum()),
                    "skipped_pnl": float(te["pnl"][skip].sum()), "kept_pnl": float(te["pnl"][~skip].sum()),
                    "all_pnl": float(te["pnl"].sum())})
    return out


def approve(wf: list[dict], holdout_from: int = 2025) -> tuple[bool, str]:
    if not wf:
        return False, "Yeterli işlem yok."
    aucs = [r["auc"] for r in wf if not math.isnan(r["auc"])]
    mean_auc = float(np.mean(aucs)) if aucs else float("nan")
    hold = [r for r in wf if r["year"] >= holdout_from]
    helped = sum(r["skipped_pnl"] < 0 for r in wf)
    if not (mean_auc >= MIN_AUC):
        return False, f"Tahmin gücü zayıf (ortalama AUC {mean_auc:.2f}, en az {MIN_AUC} gerekli)."
    if not hold or any(r["skipped_pnl"] >= 0 for r in hold):
        return False, "Son dönemde (2025→) atlayacağı işlemler toplamda kârlıydı; filtre zarar ettirirdi."
    if helped < math.ceil(len(wf) * 0.75):
        return False, f"Filtre yılların sadece {helped}/{len(wf)}'inde işe yaradı; tutarlı değil."
    return True, f"Ortalama AUC {mean_auc:.2f}; atlanacak işlemler {helped}/{len(wf)} yılda zarar ettirmişti."


def build(s: Settings, full: dict, btc: pd.DataFrame | None, start: str = "2021-01-01", progress=lambda m: None) -> dict:
    """Replay the strategy on all history, learn from every trade, test walk-forward, save the model."""
    from .validation import _params, _portfolio
    from .strategies import make_target
    progress("strateji geçmiş veride tekrar oynatılıyor")
    flt = btc if s.btc_filter else None
    targets = {k: make_target(s.strategy, d, s.allow_short, flt) for k, d in full.items()}
    r = _portfolio(full, targets, _params(s), s.max_open_positions, start)
    progress("işlemlerden öğreniliyor")
    ds = trade_dataset(full, flt, r.trades)
    last_year = int(ds["entry_time"].max().year) if len(ds) else 2026
    wf = walk_forward(ds, list(range(2022, last_year + 1)))
    ok, why = approve(wf)
    m = fit_logistic(ds[FEATURES].to_numpy(), (ds["pnl"] > 0).to_numpy(float))
    p_train = predict(m, ds[FEATURES].to_numpy())
    imp = sorted(zip(FEATURES, m["w"][1:]), key=lambda kv: -abs(kv[1]))
    out = {"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "trained_until": str(ds["exit_time"].max()) if len(ds) else None,
           "market": s.market, "strategy": s.strategy, "symbols": list(full), "trades": int(len(ds)),
           "win_rate": float((ds["pnl"] > 0).mean()) if len(ds) else None,
           "model": m, "threshold": float(np.quantile(p_train, SKIP_QUANTILE)) if len(ds) else 0.0,
           "walk_forward": wf, "approved": ok, "approval_reason": why,
           "weights": [[k, float(v)] for k, v in imp]}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = model_path(s.market).with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, default=str))
    tmp.replace(model_path(s.market))
    log.info("Öğrenen model eğitildi: %d işlem, onay=%s (%s)", len(ds), ok, why)
    return out


def load_model(market: str) -> dict | None:
    try:
        return json.loads(model_path(market).read_text())
    except Exception:
        return None


def needs_retrain(market: str) -> bool:
    m = load_model(market)
    if not m:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(m["trained_at"])
    return age.days >= RETRAIN_DAYS


# ------------------------------------------------------------------------------------------ live use
class LiveFilter:
    """Used by the live bot only when LEARNER_MODE=filter AND the model is approved.
    Never raises: on any problem the signal is allowed (the bot behaves exactly as without it)."""

    def __init__(self, s: Settings):
        self.s = s
        self.m = load_model(s.market)
        self.active = bool(self.m and self.m.get("approved") and s.learner_mode == "filter")

    def check(self, df: pd.DataFrame, btc: pd.DataFrame | None, direction: int) -> tuple[bool, float | None]:
        if not self.active:
            return True, None
        try:
            f = feature_frame(df, btc)
            x = f.iloc[-1][FEATURES].to_numpy(float).copy()
            if np.isnan(x).any():
                return True, None
            for i, k in enumerate(FEATURES):
                if k in DIRECTIONAL:
                    x[i] *= direction
            p = float(predict(self.m["model"], x)[0])
            return p >= self.m["threshold"], p
        except Exception:
            log.exception("learner check failed; allowing the signal")
            return True, None


def shadow_report(s: Settings, full: dict, btc: pd.DataFrame | None, journal: pd.DataFrame) -> list[dict]:
    """Score the bot's REAL trades (journal) with the model, as it would have at entry time."""
    m = load_model(s.market)
    if not m or journal is None or not len(journal):
        return []
    opens = journal[journal["action"] == "open"]
    closes = journal[journal["action"] == "close"]
    out = []
    frames = {}
    for o in opens.itertuples():
        sym = o.symbol
        if sym not in full:
            continue
        if sym not in frames:
            frames[sym] = feature_frame(full[sym], btc)
        t = pd.Timestamp(o.time)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        x = entry_features(frames[sym], t, int(o.direction))
        p = float(predict(m["model"], x)[0]) if x is not None else None
        c = closes[(closes["symbol"] == sym) & (pd.to_datetime(closes["time"], utc=True, format="mixed") > t)]
        pnl = float(c.iloc[0]["pnl_est"]) if len(c) else None
        out.append({"time": str(t), "symbol": sym, "direction": int(o.direction), "prob": p,
                    "would_skip": (p is not None and p < m["threshold"]), "pnl": pnl,
                    "after_training": str(t) > str(m.get("trained_until"))})
    return out
