"""Professional robustness checks for the current settings (`python -m tradingbot validate`).

What a quant desk would ask before trusting a backtest:
  1. Out-of-sample: results on 2025-26 data that was never used to choose anything
  2. Monte Carlo (stationary block bootstrap of daily returns): distribution of the NEXT 12 months -
     median, 5th / 95th percentile, probability of a loss, probability of a deep drawdown
  3. Probabilistic & Deflated Sharpe Ratio (Bailey & López de Prado 2012/2014): is the Sharpe ratio
     still significant after accounting for skew, fat tails and the ~200 variants tried in research?
  4. Probability of Backtest Overfitting, PBO (Bailey, Borwein, López de Prado, Zhu 2017) via CSCV
     over a grid of neighbouring parameter sets
  5. Cost stress (fees x2, slippage x3), parameter-sensitivity map, per-year / per-coin / regime split
  6. Losing streaks to expect (trade bootstrap)
Nothing here can promise a profit; it measures how much the backtest can be trusted.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import BacktestParams, run_portfolio
from .config import PROJECT_ROOT, Settings
from .strategies import apply_btc_filter, ema_trend, get_strategy, make_target

HOLDOUT_START = "2025-01-01"
TRIALS_IN_RESEARCH = 200  # honest count of variants tried in research/ (for the Deflated Sharpe Ratio)
# Annual Sharpe dispersion across everything tried in research/ (roughly -1.6 .. +2.0). The neighbouring
# grid below is far more homogeneous, so using its variance alone would make the DSR look too good.
TRIAL_SHARPE_STD_ANNUAL = 0.6
# The variants were highly correlated (same idea, nearby parameters), so the number of truly independent
# trials is much smaller than 200. We report both ends: N=200 (harshest) and an effective N of ~20.
EFFECTIVE_TRIALS = 20
OUT_DIR = PROJECT_ROOT / "reports"
RISK_LEVELS = (0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02)


# ------------------------------------------------------------------------------------------ stats
def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    # Acklam's rational approximation (|error| < 1.2e-9), avoids a scipy dependency
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def probabilistic_sharpe(returns: pd.Series, sr_benchmark: float = 0.0) -> float:
    """PSR: probability that the true (per-period) Sharpe exceeds sr_benchmark."""
    r = returns.dropna()
    n = len(r)
    if n < 30 or r.std() == 0:
        return float("nan")
    sr = r.mean() / r.std()
    skew = float(((r - r.mean()) ** 3).mean() / r.std() ** 3)
    kurt = float(((r - r.mean()) ** 4).mean() / r.std() ** 4)
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    return _norm_cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom)


def deflated_sharpe(returns: pd.Series, trial_srs: list[float], n_trials: int,
                    min_std_per_period: float = 0.0) -> dict:
    """DSR: PSR against the Sharpe you'd expect from the best of n_trials pure-luck strategies."""
    v = float(np.var(trial_srs, ddof=1)) if len(trial_srs) > 1 else 0.0
    v = max(v, min_std_per_period ** 2)
    g = 0.5772156649
    sr0 = math.sqrt(v) * ((1 - g) * _norm_ppf(1 - 1 / n_trials) + g * _norm_ppf(1 - 1 / (n_trials * math.e)))
    return {"sr0_per_period": sr0, "dsr": probabilistic_sharpe(returns, sr0)}


def pbo_cscv(perf: pd.DataFrame, blocks: int = 16) -> dict:
    """Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation.
    perf: T x N matrix of per-period returns, one column per configuration."""
    X = perf.dropna().to_numpy()
    T, N = X.shape
    edges = np.linspace(0, T, blocks + 1).astype(int)
    parts = [X[edges[i]:edges[i + 1]] for i in range(blocks)]
    logits = []
    for is_idx in itertools.combinations(range(blocks), blocks // 2):
        oos_idx = [i for i in range(blocks) if i not in is_idx]
        IS = np.vstack([parts[i] for i in is_idx])
        OOS = np.vstack([parts[i] for i in oos_idx])
        sr_is = IS.mean(0) / (IS.std(0) + 1e-12)
        sr_oos = OOS.mean(0) / (OOS.std(0) + 1e-12)
        best = int(np.argmax(sr_is))
        rank = (sr_oos < sr_oos[best]).sum() + 0.5 * ((sr_oos == sr_oos[best]).sum() - 1)
        w = (rank + 0.5) / N  # relative rank in (0,1)
        logits.append(math.log(w / (1 - w)))
    logits = np.array(logits)
    return {"pbo": float((logits <= 0).mean()), "combinations": len(logits),
            "median_logit": float(np.median(logits))}


def block_bootstrap(daily: pd.Series, horizon: int = 365, sims: int = 5000, block: int = 20,
                    seed: int = 7) -> dict:
    """Stationary-style block bootstrap of daily returns -> distribution of the next `horizon` days."""
    r = daily.dropna().to_numpy()
    rng = np.random.default_rng(seed)
    n = len(r)
    finals, dds = np.empty(sims), np.empty(sims)
    paths = np.empty((sims, horizon))
    for s in range(sims):
        out = []
        while len(out) < horizon:
            start = rng.integers(0, n)
            L = max(1, int(rng.geometric(1 / block)))
            out.extend(r[(start + np.arange(L)) % n])
        x = np.cumprod(1 + np.array(out[:horizon]))
        paths[s] = x
        finals[s] = x[-1] - 1
        dds[s] = (x / np.maximum.accumulate(np.r_[1.0, x])[1:] - 1).min()
    pct = lambda a, q: float(np.percentile(a, q))
    fan = {q: [float(v) for v in np.percentile(paths, q, axis=0)[::7]] for q in (5, 25, 50, 75, 95)}
    return {"median": pct(finals, 50), "p5": pct(finals, 5), "p25": pct(finals, 25), "p75": pct(finals, 75),
            "p95": pct(finals, 95), "prob_loss": float((finals < 0).mean()),
            "prob_dd_gt_20": float((dds < -0.20).mean()), "prob_dd_gt_30": float((dds < -0.30).mean()),
            "median_max_dd": pct(dds, 50), "p5_max_dd": pct(dds, 5), "fan": fan,
            "hist": np.histogram(finals, bins=40)[0].tolist(),
            "hist_edges": np.histogram(finals, bins=40)[1].tolist()}


def losing_streaks(trades: pd.DataFrame, sims: int = 5000, n_trades: int = 100, seed: int = 3) -> dict:
    if not len(trades):
        return {}
    win = (trades["pnl"] > 0).to_numpy()
    rng = np.random.default_rng(seed)
    worst = []
    for _ in range(sims):
        seq = rng.choice(win, n_trades)
        m = c = 0
        for w in seq:
            c = 0 if w else c + 1
            m = max(m, c)
        worst.append(m)
    hist = 0
    c = 0
    for w in win:
        c = 0 if w else c + 1
        hist = max(hist, c)
    return {"median_worst_streak_per_100": float(np.median(worst)), "p95_worst_streak_per_100": float(np.percentile(worst, 95)),
            "historical_worst_streak": int(hist), "win_rate": float(win.mean())}


# ------------------------------------------------------------------------------------------ runs
def _params(s: Settings, **over) -> BacktestParams:
    hours = {"m": 1 / 60, "h": 1, "d": 24}[s.timeframe[-1]] * float(s.timeframe[:-1])
    p = BacktestParams(start_equity=1000.0, fee=s.fee, slippage=s.slippage, risk_per_trade=s.risk_per_trade,
                       atr_mult=s.atr_stop_mult, trailing=s.trailing, leverage_cap=s.leverage_cap,
                       allow_short=s.allow_short, funding_rate_8h=0.0001 if s.market == "futures" else 0.0,
                       bar_hours=hours)
    return replace(p, **over)


def _portfolio(full: dict, targets: dict, p: BacktestParams, max_pos: int, start: str, end: str | None = None):
    a = pd.Timestamp(start, tz="UTC")
    b = pd.Timestamp(end, tz="UTC") if end else None
    dfs = {k: d[(d.index >= a) & ((d.index < b) if b is not None else True)] for k, d in full.items()}
    dfs = {k: d for k, d in dfs.items() if len(d) > 50}
    return run_portfolio(dfs, {k: targets[k].loc[dfs[k].index] for k in dfs}, p, max_positions=max_pos)


def _daily(equity: pd.Series) -> pd.Series:
    return equity.resample("D").last().dropna().pct_change().dropna()


def run_validation(s: Settings, full: dict[str, pd.DataFrame], btc: pd.DataFrame | None,
                   start: str = "2021-01-01", progress=lambda msg: None) -> dict:
    """full: {symbol: 4h candles with enough warm-up history}. Returns a JSON-able summary."""
    res: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "settings": {"market": s.market, "strategy": s.strategy, "profile": s.profile,
                              "risk_per_trade": s.risk_per_trade, "atr_stop_mult": s.atr_stop_mult,
                              "max_open_positions": s.max_open_positions, "btc_filter": s.btc_filter,
                              "symbols": list(full), "fee": s.fee, "slippage": s.slippage}}
    flt = btc if s.btc_filter else None
    targets = {k: make_target(s.strategy, d, s.allow_short, flt) for k, d in full.items()}
    p = _params(s)

    progress("ana backtest")
    allr = _portfolio(full, targets, p, s.max_open_positions, start)
    train = _portfolio(full, targets, p, s.max_open_positions, start, HOLDOUT_START)
    hold = _portfolio(full, targets, p, s.max_open_positions, HOLDOUT_START)
    keep = ("cagr_%", "max_drawdown_%", "sharpe", "sortino", "trades", "win_rate_%", "profit_factor",
            "avg_win_loss_ratio")
    res["periods"] = {name: {k: r.metrics[k] for k in keep} for name, r in
                      (("all", allr), ("train", train), ("holdout", hold))}
    eq = allr.equity
    yearly = (eq.resample("YE").last() / eq.resample("YE").first() - 1) * 100
    res["yearly"] = {str(d.year): float(v) for d, v in yearly.items()}
    monthly = eq.resample("ME").last().pct_change().dropna()
    res["monthly"] = {"positive_share": float((monthly > 0).mean()), "best": float(monthly.max() * 100),
                      "worst": float(monthly.min() * 100), "count": int(len(monthly))}
    res["equity"] = [[str(t.date()), float(v)] for t, v in eq.resample("D").last().dropna().items()]

    # per coin contribution and regime split
    tr = allr.trades
    if len(tr):
        res["per_coin"] = {k: {"pnl": float(g.pnl.sum()), "trades": int(len(g)), "win_rate": float((g.pnl > 0).mean())}
                           for k, g in tr.groupby("symbol")}
        if btc is not None:
            reg = (btc.close > btc.close.ewm(span=200, adjust=False).mean()).reindex(
                pd.to_datetime(tr.entry_time), method="ffill")
            tr = tr.assign(btc_up=reg.to_numpy())
            res["regime"] = {("btc_up" if k else "btc_down"): {"pnl": float(g.pnl.sum()), "trades": int(len(g))}
                             for k, g in tr.groupby("btc_up")}
        res["streaks"] = losing_streaks(allr.trades)

    progress("Monte Carlo")
    d_all, d_hold = _daily(eq), _daily(hold.equity)
    res["mc_all"] = block_bootstrap(d_all)
    res["mc_holdout"] = block_bootstrap(d_hold) if len(d_hold) > 120 else None

    progress("maliyet stresi")
    stress = _portfolio(full, targets, replace(p, fee=p.fee * 2, slippage=p.slippage * 3), s.max_open_positions, start)
    res["cost_stress"] = {k: stress.metrics[k] for k in ("cagr_%", "max_drawdown_%", "sharpe")}

    progress("risk seviyeleri")
    curve = []
    for rk in RISK_LEVELS:
        ra = _portfolio(full, targets, replace(p, risk_per_trade=rk), s.max_open_positions, start)
        rh = _portfolio(full, targets, replace(p, risk_per_trade=rk), s.max_open_positions, HOLDOUT_START)
        mc = block_bootstrap(_daily(rh.equity), sims=1500) if len(rh.equity) > 720 else None
        curve.append({"risk": rk, "cagr": ra.metrics["cagr_%"], "dd": ra.metrics["max_drawdown_%"],
                      "sharpe": ra.metrics["sharpe"], "holdout_cagr": rh.metrics["cagr_%"],
                      "holdout_dd": rh.metrics["max_drawdown_%"],
                      "prob_loss_12m": mc["prob_loss"] if mc else None,
                      "prob_dd_gt_20": mc["prob_dd_gt_20"] if mc else None,
                      "p5_12m": mc["p5"] if mc else None})
    res["risk_curve"] = curve

    progress("parametre haritası + PBO")
    grid_rets, grid = {}, []
    spec = get_strategy(s.strategy)
    pairs = [(10, 30), (20, 50), (30, 100), (50, 150)] if spec.name == "ema_trend" else [(None, None)]
    for (fast, slow), atr_m in itertools.product(pairs, [3.0, 4.0, 5.0, 6.0]):
        if fast is None:
            tg = targets
        else:
            tg = {k: (apply_btc_filter(ema_trend(d, s.allow_short, fast=fast, slow=slow), flt)
                      if flt is not None else ema_trend(d, s.allow_short, fast=fast, slow=slow))
                  for k, d in full.items()}
        r = _portfolio(full, tg, replace(p, atr_mult=atr_m), s.max_open_positions, start)
        h = _portfolio(full, tg, replace(p, atr_mult=atr_m), s.max_open_positions, HOLDOUT_START)
        label = f"{fast}/{slow} · {atr_m:g}×ATR" if fast else f"{atr_m:g}×ATR"
        grid.append({"ema": f"{fast}/{slow}" if fast else "-", "atr": atr_m, "sharpe": r.metrics["sharpe"],
                     "cagr": r.metrics["cagr_%"], "dd": r.metrics["max_drawdown_%"],
                     "holdout_sharpe": h.metrics["sharpe"], "label": label})
        grid_rets[label] = _daily(r.equity)
    res["grid"] = grid
    perf = pd.DataFrame(grid_rets)
    res["pbo"] = pbo_cscv(perf)
    per_period_srs = [float(c.mean() / c.std()) for _, c in perf.items() if c.std() > 0]
    res["psr"] = probabilistic_sharpe(d_all)
    sd = TRIAL_SHARPE_STD_ANNUAL / math.sqrt(365)
    res["dsr"] = deflated_sharpe(d_all, per_period_srs, TRIALS_IN_RESEARCH, min_std_per_period=sd)
    res["dsr"]["sr0_annual"] = res["dsr"]["sr0_per_period"] * math.sqrt(365)
    eff = deflated_sharpe(d_all, per_period_srs, EFFECTIVE_TRIALS, min_std_per_period=sd)
    res["dsr"]["dsr_effective"] = eff["dsr"]
    res["dsr"]["sr0_annual_effective"] = eff["sr0_per_period"] * math.sqrt(365)
    res["psr_holdout"] = probabilistic_sharpe(d_hold)
    res["verdict"] = verdict(res)
    return res


def verdict(r: dict) -> list[dict]:
    """Plain-language traffic lights: state = ok | warn | info."""
    out = []
    h = r["periods"]["holdout"]
    out.append({"state": "ok" if h["sharpe"] > 0.5 and h["cagr_%"] > 0 else "warn",
                "title": "Saklı dönemde (2025-26) çalışıyor mu?",
                "text": f"Yıllık %{h['cagr_%']:.1f}, en kötü düşüş -%{abs(h['max_drawdown_%']):.1f}, Sharpe {h['sharpe']:.2f}. "
                        f"Eğitim dönemindeki %{r['periods']['train']['cagr_%']:.0f}'in çok altında; gerçekçi beklenti "
                        f"budur. Not: bu dönem tamamen temiz değil (stop mesafesi ilk taramada tüm veriyle seçildi, "
                        f"BTC filtresi bu dönemde de kontrol edildi). Tek gerçekten temiz test: bundan sonraki "
                        f"paper/demo sonuçları."})
    d = r["dsr"]
    out.append({"state": "ok" if d["dsr"] > 0.9 else ("info" if d["dsr_effective"] > 0.75 else "warn"),
                "title": "Şans eseri mi? (Deflated Sharpe Ratio)",
                "text": f"Araştırmada ~{TRIALS_IN_RESEARCH} varyant denendi. Hepsi birbirinden bağımsız sayılırsa "
                        f"(en sert varsayım) avantajın gerçek olma olasılığı %{d['dsr'] * 100:.0f}; denemeler birbirine "
                        f"çok benzediği için gerçekçi etkin sayı ~{EFFECTIVE_TRIALS} alınırsa %{d['dsr_effective'] * 100:.0f}. "
                        f"Saklı dönemde Sharpe'ın sıfırdan büyük olma olasılığı %{r['psr_holdout'] * 100:.0f}."})
    pb = r["pbo"]["pbo"]
    out.append({"state": "info", "title": "İnce ayar işe yarıyor mu? (PBO)",
                "text": f"Komşu ayarlar arasından geçmişte en iyi olanı seçmek, gelecekte %{pb * 100:.0f} olasılıkla "
                        f"ortalamanın altında kalıyor. Yani ayarlar arasında 'en iyiyi' kovalamak fayda sağlamıyor. "
                        f"Bot bu yüzden sabit, sağlam bir ayar kullanıyor ve sık sık yeniden optimize edilmiyor."})
    c = r["cost_stress"]
    out.append({"state": "ok" if c["cagr_%"] > 0 else "warn", "title": "Ücretler 2 kat, kayma 3 kat olursa?",
                "text": f"Yıllık %{c['cagr_%']:.1f}, Sharpe {c['sharpe']:.2f}. "
                        + ("Strateji maliyete dayanıklı." if c["cagr_%"] > 0 else "Maliyetler kârı siliyor.")})
    sens = [g["sharpe"] for g in r["grid"]]
    prof = sum(g["cagr"] > 0 for g in r["grid"])
    hold_pos = sum(g["holdout_sharpe"] > 0 for g in r["grid"])
    out.append({"state": "ok" if min(sens) > 0.5 and prof == len(sens) else "warn",
                "title": "Komşu ayarlar da çalışıyor mu?",
                "text": f"{len(sens)} farklı EMA / stop kombinasyonundan {prof} tanesi kârlı (Sharpe {min(sens):.2f} - "
                        f"{max(sens):.2f}); saklı dönemde {hold_pos} tanesi kârlı. "
                        + ("Sonuç tek bir şanslı ayara bağlı değil." if prof == len(sens) else
                           "Sonuç ayara duyarlı, dikkatli ol.")})
    mc = r["mc_holdout"] or r["mc_all"]
    out.append({"state": "warn" if mc["prob_loss"] > 0.25 else "info", "title": "Önümüzdeki 12 ay, kötümser senaryo",
                "text": f"Son 2 yılın getirileri tekrar ederse: ortanca %{mc['median'] * 100:.1f}, zararla kapatma "
                        f"olasılığı %{mc['prob_loss'] * 100:.0f}, %20'den derin düşüş olasılığı "
                        f"%{mc['prob_dd_gt_20'] * 100:.0f}."})
    cur = next((c for c in r.get("risk_curve") or [] if abs(c["risk"] - r["settings"]["risk_per_trade"]) < 1e-9), None)
    if cur and cur["prob_dd_gt_20"] is not None:
        hi = cur["prob_dd_gt_20"] > 0.15
        out.append({"state": "warn" if hi else "ok", "title": "Risk seviyesi uygun mu?",
                    "text": f"İşlem başı %{cur['risk'] * 100:.2f} risk ile kötümser senaryoda 12 ayda %20'den derin "
                            f"düşüş olasılığı %{cur['prob_dd_gt_20'] * 100:.0f}. "
                            + ("Bu yüksek; riski düşürmeyi düşün." if hi else "Makul seviyede.")})
    st = r.get("streaks") or {}
    if st:
        out.append({"state": "info", "title": "Ard arda kayıp beklentisi",
                    "text": f"İsabet %{st['win_rate'] * 100:.0f}. 100 işlemde ard arda ~{st['median_worst_streak_per_100']:.0f} "
                            f"kayıp normal, {st['p95_worst_streak_per_100']:.0f}'ye kadar görülebilir. Geçmişte en uzun seri: "
                            f"{st['historical_worst_streak']} (birden fazla coin aynı anda stop olduğunda)."})
    return out


def load_for(s: Settings, since: str = "2020-01-01") -> tuple[dict, pd.DataFrame | None]:
    from .data import load_history
    full = {}
    for sym in s.symbols:
        try:
            full[sym] = load_history(s, sym, s.timeframe, since)
        except Exception:
            pass
    btc = load_history(s, s.btc_symbol, s.timeframe, since) if s.btc_filter else None
    return full, btc


def _fan_at(fan: dict, day: float, q: int) -> float:
    """Return (not wealth) at `day` for percentile q from the weekly fan (index k = day 7k+1)."""
    arr = fan.get(q, fan.get(str(q)))
    k = max(0.0, (min(day, 365) - 1) / 7)
    i = min(int(k), len(arr) - 1)
    j = min(i + 1, len(arr) - 1)
    frac = k - int(k) if j > i else 0.0
    x = arr[i] + (arr[j] - arr[i]) * frac
    if day < 1:  # linear from 0 at day 0
        x = 1 + (arr[0] - 1) * max(day, 0)
    return float(x - 1)


def live_tracking(res: dict, state_dir: Path | None = None) -> list[dict]:
    """Compare what the bot actually did (state/equity_<mode>_<market>.csv) with the Monte Carlo band.
    This is the only truly out-of-sample test: data that did not exist when the strategy was chosen."""
    sd = Path(state_dir) if state_dir else PROJECT_ROOT / "state"
    market = res["settings"]["market"]
    mc = res.get("mc_holdout") or res["mc_all"]
    out = []
    for f in sorted(sd.glob(f"equity_*_{market}.csv")):
        mode = f.stem.split("_")[1]
        try:
            eq = pd.read_csv(f)
            t = pd.to_datetime(eq["time"], utc=True, format="mixed")
            v = eq["equity"].astype(float)
        except Exception:
            continue
        if len(v) < 2 or v.iloc[0] <= 0:
            continue
        days = (t.iloc[-1] - t.iloc[0]).total_seconds() / 86400
        ret = float(v.iloc[-1] / v.iloc[0] - 1)
        dd = float((v / v.cummax() - 1).min())
        band = {q: _fan_at(mc["fan"], days, q) for q in (5, 25, 50, 75, 95)}
        row = {"mode": mode, "start": str(t.iloc[0])[:10], "days": round(days, 1), "return": ret, "max_dd": dd,
               "equity": float(v.iloc[-1]), "band": band, "dd_p5_12m": mc["p5_max_dd"]}
        if days < 14:
            row.update(state="info", text="Henüz çok erken (2 haftadan az). Birkaç hafta sonra anlamlı olur.")
        elif dd < mc["p5_max_dd"]:
            row.update(state="warn", text="Düşüş, simülasyonlardaki en kötü %5'lik dilimden de derin. Piyasa "
                                          "değişmiş olabilir: botu durdurup sonuçları incele / bana gönder.")
        elif ret < band[5]:
            row.update(state="warn", text="Getiri beklenen aralığın altında (en kötü %5'lik dilim). Bir süre daha "
                                          "böyle giderse strateji bu piyasada çalışmıyor olabilir; incele.")
        elif ret < band[25]:
            row.update(state="info", text="Zayıf ama normal aralıkta. Trend stratejileri uzun yatay dönemler yaşar.")
        elif ret > band[95]:
            row.update(state="info", text="Beklenenden çok daha iyi. Büyük kısmı şans olabilir; riski artırma.")
        else:
            row.update(state="ok", text="Beklenen aralıkta.")
        out.append(row)
    return out


def paths(market: str) -> tuple[Path, Path]:
    return OUT_DIR / f"validation_{market}.json", OUT_DIR / f"validation_{market}.html"


def load_saved(market: str) -> dict | None:
    j = paths(market)[0]
    try:
        return json.loads(j.read_text())
    except Exception:
        return None


def save(res: dict, state_dir: Path | None = None) -> tuple[Path, Path]:
    """Writes reports/validation_<market>.json + .html (live tracking is refreshed on every call)."""
    from .validation_report import render
    res["live"] = live_tracking(res, state_dir)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    j, h = paths(res["settings"]["market"])
    tmp = j.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=1, default=float))
    tmp.replace(j)
    h.write_text(render(res), encoding="utf-8")
    return j, h


def stale_reason(res: dict, s: Settings) -> str | None:
    """Why a saved result no longer matches the current settings (None = still valid)."""
    old = res.get("settings", {})
    diffs = []
    for key, now in (("strategy", s.strategy), ("risk_per_trade", s.risk_per_trade),
                     ("atr_stop_mult", s.atr_stop_mult), ("max_open_positions", s.max_open_positions),
                     ("btc_filter", s.btc_filter)):
        if old.get(key) != now:
            diffs.append(key)
    if sorted(old.get("symbols", [])) != sorted(s.symbols):
        diffs.append("symbols")
    return ("Ayarlar değişti (" + ", ".join(diffs) + "); testleri yeniden çalıştır.") if diffs else None
