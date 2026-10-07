"""ML agent (experimental, PAPER or Binance DEMO only, never a real account): a gradient-boosted tree model trained on every hourly candle
since 2017 for the selected coins. Each hour it estimates, per coin, the probability that the next 24
hours return more than the round-trip cost; it buys only when that probability is >= 65% and sells
24 hours later. It retrains on all data (including the newest weeks) once a week.

Research (research/ml_agent.py, walk-forward, retrained each quarter on past data only, fees 0.3%
round trip, entry at the next hour's open): 2025 +38%, 2026 YTD +2%, 2022 -20%; worst in-year drop
about -40% in 2020-22, under -10% since 2023. It is NOT connected to any real or demo account:
it trades with simulated money at real Binance prices so you can watch it before trusting it.
"""
from __future__ import annotations

import json
import logging
import pickle
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT, Settings
from .journal import Journal

log = logging.getLogger(__name__)
STATE_DIR = PROJECT_ROOT / "state"
HORIZON = 24                 # hours a position is held
THRESHOLD = 0.65             # minimum predicted probability to buy
FEE, SLIP = 0.001, 0.0005    # per side (spot taker fee + slippage)
COST = 2 * (FEE + SLIP)
HISTORY_SINCE = "2017-08-01"
LIVE_BARS_DAYS = 120         # hourly history needed for the slowest features (EMA1000, 30-day return)
START_CASH = 1000.0
RETRAIN_DAYS = 7
FEATURES = ["r1", "r4", "r12", "r24", "r72", "r168", "r720", "vol24", "vol168", "volr", "rsi", "g2050",
            "g200", "g1000", "vz", "range", "hour", "dow", "btc_r24", "btc_r168", "btc_ema200",
            "xs_r24", "xs_r168"]


def _paths(account: str = "paper"):
    if account not in ("paper", "demo"):
        raise ValueError("ML agent account must be paper or demo")
    return (STATE_DIR / "mlagent_model.pkl", STATE_DIR / "mlagent_meta.json", STATE_DIR / f"mlagent_{account}.json",
            STATE_DIR / f"journal_ml_{account}.csv", STATE_DIR / f"equity_ml_{account}.csv")


def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(c, n=14):
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    lo = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / lo)


def features(data: dict[str, pd.DataFrame], btc: pd.DataFrame, with_label: bool = False) -> pd.DataFrame:
    """One row per (hour, coin), using only candles up to that hour's close. `fwd` = label (future)."""
    b = btc["close"]
    lb = np.log(b)
    btcf = pd.DataFrame({"btc_r24": lb.diff(24), "btc_r168": lb.diff(168), "btc_ema200": b / _ema(b, 200) - 1})
    rows = []
    for sym, d in data.items():
        c = d["close"]
        lc = np.log(c)
        r1 = lc.diff()
        f = pd.DataFrame(index=d.index)
        for n in (1, 4, 12, 24, 72, 168, 720):
            f[f"r{n}"] = lc.diff(n)
        f["vol24"], f["vol168"] = r1.rolling(24).std(), r1.rolling(168).std()
        f["volr"] = f["vol24"] / f["vol168"]
        f["rsi"] = _rsi(c)
        f["g2050"] = _ema(c, 20) / _ema(c, 50) - 1
        f["g200"] = c / _ema(c, 200) - 1
        f["g1000"] = c / _ema(c, 1000) - 1
        lv = np.log(d["volume"] + 1)
        f["vz"] = lv - lv.rolling(168).mean()
        f["range"] = (d["high"] - d["low"]) / c
        f["hour"], f["dow"] = d.index.hour, d.index.dayofweek
        f = f.join(btcf)
        if with_label:
            f["fwd"] = lc.shift(-HORIZON) - lc
        f["coin"] = sym
        rows.append(f)
    X = pd.concat(rows).dropna(subset=["r720", "g1000", "btc_ema200"])
    X["xs_r24"] = X.groupby(level=0)["r24"].rank(pct=True)
    X["xs_r168"] = X.groupby(level=0)["r168"].rank(pct=True)
    return X.sort_index()


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=31,
                                          l2_regularization=1.0, min_samples_leaf=200, random_state=0)


def _paper_check(P: pd.DataFrame) -> dict:
    """Simulate the trading rule on scored rows (P: p, fwd, coin) -> return, drawdown, trades."""
    trades = []
    for coin, g in P[P["p"] >= THRESHOLD].groupby("coin"):
        free = None
        for t, r in g.iterrows():
            if free is not None and t < free:
                continue
            trades.append((t + pd.Timedelta(hours=HORIZON), float(np.expm1(r["fwd"]) - COST)))
            free = t + pd.Timedelta(hours=HORIZON)
    if not trades:
        return {"trades": 0, "return_pct": 0.0, "max_dd_pct": 0.0, "win_rate": None}
    T = pd.DataFrame(trades, columns=["exit", "ret"]).set_index("exit").sort_index()
    n = P["coin"].nunique()
    daily = (T["ret"] / n).resample("D").sum()
    eq = (1 + daily).cumprod()
    return {"trades": int(len(T)), "return_pct": float((eq.iloc[-1] - 1) * 100),
            "max_dd_pct": float(((eq / eq.cummax()).min() - 1) * 100), "win_rate": float((T["ret"] > 0).mean())}


def train(data: dict[str, pd.DataFrame], btc: pd.DataFrame, progress=lambda m: None) -> dict:
    """Out-of-sample check on the last 12 months (model trained only on data before), then the final
    model on everything. Both are saved to state/."""
    from sklearn.metrics import roc_auc_score
    progress("özellikler hesaplanıyor")
    X = features(data, btc, with_label=True)
    X["y"] = (X["fwd"] > COST).astype(int)
    lab = X.dropna(subset=["fwd"])
    end = lab.index.max()
    cut = end - pd.Timedelta(days=365)
    progress("son 12 ay için sınama (model o dönemi görmeden)")
    tr = lab[lab.index < cut - pd.Timedelta(hours=HORIZON)].iloc[::3]
    te = lab[lab.index >= cut]
    check = {}
    if len(tr) > 5000 and len(te) > 1000:
        m0 = _model().fit(tr[FEATURES], tr["y"])
        p = m0.predict_proba(te[FEATURES])[:, 1]
        check = {"from": str(cut.date()), "to": str(end.date()), "auc": float(roc_auc_score(te["y"], p)),
                 **_paper_check(pd.DataFrame({"p": p, "fwd": te["fwd"], "coin": te["coin"]}, index=te.index))}
        bh = np.mean([float(d["close"][d.index >= cut].iloc[-1] / d["close"][d.index >= cut].iloc[0] - 1)
                      for d in data.values() if (d.index >= cut).sum() > 10]) * 100
        check["buy_hold_pct"] = float(bh)
    progress("son model tüm veriyle eğitiliyor")
    m = _model().fit(lab[FEATURES].iloc[::3], lab["y"].iloc[::3])
    meta = {"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_from": str(lab.index.min().date()), "data_to": str(end), "samples": int(len(lab)),
            "coins": sorted(data), "horizon_h": HORIZON, "threshold": THRESHOLD, "cost_round_trip": COST,
            "holdout_check": check}
    mp, metap = _paths()[:2]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(mp.with_suffix(".tmp"), "wb") as fh:
        pickle.dump(m, fh)
    mp.with_suffix(".tmp").replace(mp)
    metap.write_text(json.dumps(meta, indent=1))
    log.info("ML ajan eğitildi: %d örnek, son 12 ay AUC %s", len(lab), check.get("auc"))
    return meta


def load_meta() -> dict | None:
    try:
        return json.loads(_paths()[1].read_text())
    except Exception:
        return None


def needs_retrain() -> bool:
    m = load_meta()
    if not m:
        return True
    return (datetime.now(timezone.utc) - datetime.fromisoformat(m["trained_at"])).days >= RETRAIN_DAYS


def paper_settings(s: Settings) -> Settings:
    """Real Binance spot prices, no keys: the agent never touches any account."""
    return Settings(mode="paper", market="spot", symbols=list(s.symbols)).validate()


def load_training_data(s: Settings, progress=lambda m: None) -> tuple[dict, pd.DataFrame]:
    from .data import load_history
    ps = paper_settings(s)
    data = {}
    for i, sym in enumerate(ps.symbols):
        progress(f"saatlik veri indiriliyor {sym} ({i + 1}/{len(ps.symbols)})")
        try:
            data[sym] = load_history(ps, sym, "1h", HISTORY_SINCE)
        except Exception as e:
            log.warning("%s 1h verisi alınamadı: %s", sym, e)
    btc = data.get("BTC/USDT")
    if btc is None:
        btc = load_history(ps, "BTC/USDT", "1h", HISTORY_SINCE)
    return data, btc


def demo_settings(env: dict) -> Settings:
    """Binance DEMO spot with the demo keys. Never the real account, whatever MODE the app is in."""
    from .config import settings_from
    return settings_from({**env, "MODE": "demo", "MARKET": "spot", "LIVE_TRADING_CONFIRM": ""})


class MLAgent:
    """Hourly loop. account='paper': simulated fills at real Binance prices.
    account='demo': real orders on the Binance DEMO account (fake money), limited to `budget` USDT.
    State: state/mlagent_<account>.json; journal: state/journal_ml_<account>.csv."""

    def __init__(self, s: Settings, exchange=None, account: str = "paper", budget: float = START_CASH):
        from .exchange import make_exchange
        self.account = account
        if account == "demo":
            if s.mode != "demo" or s.market != "spot":
                raise RuntimeError("ML ajan demo hesapta sadece Binance Demo spot ile çalışır.")
            self.s = s
            self.ex = exchange or make_exchange(s)
            if exchange is None:
                self.ex.load_markets()
        else:
            self.s = paper_settings(s)
            self.ex = exchange or make_exchange(self.s, authenticated=False)
        self.budget = float(budget)
        mp, _, self.state_path, jp, ep = _paths(account)
        if not mp.exists():
            raise RuntimeError("ML ajan modeli yok. Önce 'Eğit' butonuna bas.")
        with open(mp, "rb") as fh:
            self.model = pickle.load(fh)
        self.journal = Journal(jp, ep)
        self.state = self._load()
        self.status = {"state": "starting", "equity": None, "error": None, "loops": 0, "last_loop": None}
        self.last_preds: dict[str, float] = self.state.get("last_preds", {})

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {"cash": self.budget, "positions": {}, "last_hour": None, "last_preds": {}}

    def _buy(self, sym: str, spend: float) -> tuple[float, float, float]:
        """-> (qty, average price, USDT spent incl. fee)."""
        if self.account == "demo":
            from .execution import execute
            px = self.price(sym)
            free = float((self.ex.fetch_balance().get("free") or {}).get("USDT") or 0)
            spend = min(spend, free * 0.98)
            if spend < 10:
                return 0.0, 0.0, 0.0
            q = float(self.ex.amount_to_precision(sym, spend / px * (1 - FEE)))
            filled, avg = execute(self.ex, sym, "buy", q)
            return filled * (1 - FEE), avg, filled * avg       # Binance takes the fee from the coin bought
        px = self.price(sym) * (1 + SLIP)
        return spend * (1 - FEE) / px, px, spend

    def _sell(self, sym: str, qty: float) -> tuple[float, float]:
        """-> (average price, USDT received after fee). Sells only this agent's own quantity."""
        if self.account == "demo":
            from .execution import execute
            base = sym.split("/")[0]
            have = float((self.ex.fetch_balance().get("free") or {}).get(base) or 0)
            q = float(self.ex.amount_to_precision(sym, min(qty, have)))
            if q <= 0:
                return self.price(sym), 0.0
            filled, avg = execute(self.ex, sym, "sell", q)
            return avg, filled * avg * (1 - FEE)
        px = self.price(sym) * (1 - SLIP)
        return px, qty * px * (1 - FEE)

    def _save(self) -> None:
        self.state["last_preds"] = self.last_preds
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    def _candles(self, sym: str) -> pd.DataFrame:
        from .exchange import ohlcv_to_df
        since = self.ex.milliseconds() - LIVE_BARS_DAYS * 86400_000
        rows = []
        while True:
            batch = self.ex.fetch_ohlcv(sym, "1h", since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 1000:
                break
            since = batch[-1][0] + 3600_000
        df = ohlcv_to_df(rows)
        if len(df) and int(df.index[-1].timestamp() * 1000) + 3600_000 > self.ex.milliseconds():
            df = df.iloc[:-1]                      # drop the forming candle
        return df

    def price(self, sym: str) -> float:
        return float(self.ex.fetch_ticker(sym)["last"])

    def equity(self, prices: dict | None = None) -> float:
        e = float(self.state["cash"])
        for sym, p in self.state["positions"].items():
            px = (prices or {}).get(sym) or self.price(sym)
            e += p["qty"] * px
        return e

    def step(self, now: pd.Timestamp | None = None) -> None:
        now = now or pd.Timestamp.now(tz="UTC")
        data = {sym: self._candles(sym) for sym in self.s.symbols}
        btc = data.get("BTC/USDT")
        if btc is None:
            btc = self._candles("BTC/USDT")
        last_hour = str(max(d.index[-1] for d in data.values() if len(d)))
        prices = {sym: float(d["close"].iloc[-1]) for sym, d in data.items() if len(d)}
        # exits: hold exactly HORIZON hours
        for sym, p in list(self.state["positions"].items()):
            if now >= pd.Timestamp(p["exit_due"]):
                px, proceeds = self._sell(sym, p["qty"])
                self.state["cash"] += proceeds
                pnl = proceeds - p["cost"]
                del self.state["positions"][sym]
                self.journal.trade("close", sym, 1, p["qty"], px, 0, "24 saat doldu", pnl, p["entry"], self.equity(prices))
                log.info("ML %s SAT @ %.6g (%+.2f USDT)", sym, px, pnl)
        # entries, once per closed hour
        if last_hour != self.state.get("last_hour"):
            X = features(data, btc)
            latest = X[X.index == X.index.max()]
            if len(latest):
                probs = self.model.predict_proba(latest[FEATURES])[:, 1]
                self.last_preds = {c: float(p) for c, p in zip(latest["coin"], probs)}
            eq = self.equity(prices)
            slot = eq / len(self.s.symbols)
            for sym, p in sorted(self.last_preds.items(), key=lambda kv: -kv[1]):
                if p < THRESHOLD or sym in self.state["positions"]:
                    continue
                spend = min(slot, self.state["cash"])
                if spend < 10:
                    continue
                qty, px, spend = self._buy(sym, spend)
                if qty <= 0:
                    continue
                self.state["cash"] -= spend
                self.state["positions"][sym] = {"qty": qty, "entry": px, "cost": spend, "prob": p,
                                                "opened": now.isoformat(),
                                                "exit_due": (now + pd.Timedelta(hours=HORIZON)).isoformat()}
                self.journal.trade("open", sym, 1, qty, px, 0, f"olasılık %{p * 100:.0f}", 0.0, px, eq)
                log.info("ML %s AL @ %.6g (olasılık %%%.0f)", sym, px, p * 100)
            self.state["last_hour"] = last_hour
        eq = self.equity(prices)
        self.journal.equity(eq, len(self.state["positions"]))
        self.status.update(equity=eq)
        self._save()

    def run(self, poll_seconds: int = 60, stop_event=None) -> None:
        log.info("ML ajan başladı (%s, %d coin, eşik %%%.0f, %d saat tutma)", self.account.upper(), len(self.s.symbols),
                 THRESHOLD * 100, HORIZON)
        while not (stop_event and stop_event.is_set()):
            try:
                self.step()
                self.status.update(state="running", error=None, loops=self.status["loops"] + 1,
                                   last_loop=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            except Exception as e:
                log.exception("ML ajan döngü hatası (tekrar denenecek)")
                self.status.update(error=f"{type(e).__name__}: {str(e)[:200]}")
            for _ in range(poll_seconds):
                if stop_event and stop_event.is_set():
                    break
                time.sleep(1)
        self.status["state"] = "stopped"

    def flatten(self) -> None:
        for sym, p in list(self.state["positions"].items()):
            px, proceeds = self._sell(sym, p["qty"])
            self.state["cash"] += proceeds
            self.journal.trade("close", sym, 1, p["qty"], px, 0, "elle kapatma", proceeds - p["cost"], p["entry"])
            del self.state["positions"][sym]
        self._save()

    def reset(self) -> None:
        for f in (self.state_path,):
            f.write_text(json.dumps({"cash": self.budget, "positions": {}, "last_hour": None, "last_preds": {}}))


def learning_curve(journal: pd.DataFrame, expected_win: float | None = None) -> dict:
    """How the agent is doing over time on its own closed trades: weekly hit rate and average result,
    plus the rolling 20-trade hit rate, to compare with what the out-of-sample test predicted."""
    if journal is None or not len(journal) or "action" not in journal.columns:
        return {"weeks": [], "rolling": [], "expected_win": expected_win}
    c = journal[journal["action"] == "close"].copy()
    if not len(c):
        return {"weeks": [], "rolling": [], "expected_win": expected_win}
    c["t"] = pd.to_datetime(c["time"], utc=True, format="mixed")
    c["pnl"] = c["pnl_est"].astype(float)
    c["entry"] = c["entry"].astype(float)
    c["ret"] = c["pnl"] / (c["qty"].astype(float) * c["entry"]).replace(0, np.nan)
    weeks = []
    for wk, g in c.groupby(c["t"].dt.to_period("W")):
        weeks.append({"week": str(wk.start_time.date()), "trades": int(len(g)), "win_rate": float((g["pnl"] > 0).mean()),
                      "avg_ret_pct": float(g["ret"].mean() * 100), "pnl": float(g["pnl"].sum())})
    roll = (c["pnl"] > 0).astype(float).rolling(20, min_periods=5).mean()
    return {"weeks": weeks, "expected_win": expected_win, "total": int(len(c)),
            "win_rate": float((c["pnl"] > 0).mean()),
            "rolling": [[str(t), float(v)] for t, v in zip(c["t"], roll) if not np.isnan(v)]}
