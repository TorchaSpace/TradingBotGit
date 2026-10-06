"""Bigger ML: learn WHICH trend signals are likely to win (meta-labeling).

Data: every ema_trend trade on 24 coins, spot + futures, 2019-2026 (~10,000 trades), 19 features
incl. BTC context and market breadth (% of coins above their EMA200).
Model: gradient boosting. Honest protocol: tune on <2023, validate 2023-24, final check 2025-26.
Needs: pip install scikit-learn

    python research/meta_labeling.py

Result (2026-10): the model DOES see something (AUC 0.60 validation, 0.66 holdout; 0.5 = random,
the plain direction model in ml_experiment.py was 0.51). But using it inside the portfolio
was not consistently better: skipping weak signals lowered spot returns in 2023-24, sizing up
strong ones helped futures in 2025-26 (+5% CAGR) but raised drawdown. -> not enabled; re-run
this as more data accumulates.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from _common import END, SPLIT, WIDE, fmt, params, portfolio, universe
from tradingbot.backtest import run_backtest
from tradingbot.indicators import adx, atr, ema, higher_tf_trend, rsi
from tradingbot.strategies import make_target


def feats(df, btc, breadth):
    c, a = df.close, atr(df)
    X = pd.DataFrame(index=df.index)
    for k in (6, 24, 96, 240):
        X[f"r{k}"] = np.log(c / c.shift(k))
    X["rsi"], X["atrp"] = rsi(c), a / c
    X["atrp_rel"] = X.atrp / X.atrp.rolling(500).median()
    for n in (20, 50, 200):
        X[f"d{n}"] = (c - ema(c, n)) / a
    X["adx"], X["htf"] = adx(df), higher_tf_trend(df)
    X["vz"] = (df.volume - df.volume.rolling(120).mean()) / df.volume.rolling(120).std()
    X["hi_dist"] = (c - c.rolling(500).max()) / a
    b = btc.reindex(df.index).ffill()
    X["btc_r24"], X["btc_r96"] = np.log(b / b.shift(24)), np.log(b / b.shift(96))
    X["btc_d200"] = np.log(b / ema(btc, 200).reindex(df.index).ffill())
    X["rel96"] = X.r96 - X.btc_r96
    X["breadth"] = breadth.reindex(df.index).ffill()
    return X


model = lambda: HistGradientBoostingClassifier(max_iter=300, learning_rate=0.03, max_leaf_nodes=8,
                                               min_samples_leaf=80, l2_regularization=2.0)
rows, data = [], {}
for m in ("spot", "futures"):
    full = universe(m, WIDE)
    btc = full["BTC"]
    above = pd.concat({b: (d.close > ema(d.close, 200)).astype(float).where(ema(d.close, 200).notna())
                       for b, d in full.items()}, axis=1)
    X = {b: feats(d, btc.close, above.mean(axis=1)) for b, d in full.items()}
    T = {b: make_target("ema_trend", d, m == "futures", btc) for b, d in full.items()}
    data[m] = (full, X, T)
    for b, d in full.items():
        tr = run_backtest(d, T[b], params(m, atr_mult=5, trailing=False, risk_per_trade=0.005)).trades
        if not len(tr):
            continue
        sig_t = [d.index[d.index.get_loc(t) - 1] for t in tr.entry_time]
        f = X[b].loc[sig_t].reset_index(drop=True)
        f["dir"], f["market"] = tr.direction.values, int(m == "futures")
        f["y"], f["pnl"], f["time"] = (tr.pnl.values > 0).astype(int), tr.pnl.values, tr.entry_time.values
        rows.append(f)
D = pd.concat(rows).dropna(subset=["rsi", "d200"]).reset_index(drop=True)
D["time"] = pd.to_datetime(D.time, utc=True)
F = [c for c in D.columns if c not in ("y", "pnl", "time")]
print(f"{len(D)} trades, win rate {D.y.mean():.1%}")


def scale(m, mdl, lo_thr, hi_thr):
    full, X, T = data[m]
    out = {}
    for b, x in X.items():
        x = x.copy()
        x["dir"], x["market"] = np.sign(T[b]).replace(0, 1), int(m == "futures")
        ok = x[F].notna().all(axis=1)
        p = pd.Series(np.nan, index=x.index)
        p[ok] = mdl.predict_proba(x.loc[ok, F])[:, 1]
        s = pd.Series(1.0, index=x.index)
        s[p < lo_thr], s[p >= hi_thr] = 0.5, 1.5
        out[b] = s
    return out


for label, cut, start, end in (("VALID 2023-24", "2023-01-01", "2023-01-01", SPLIT),
                               ("HOLDOUT 2025-26", SPLIT, SPLIT, END)):
    tr = D[D.time < pd.Timestamp(cut, tz="UTC")]
    te = D[(D.time >= pd.Timestamp(start, tz="UTC")) & (D.time < pd.Timestamp(end, tz="UTC"))]
    mdl = model().fit(tr[F], tr.y)
    pt = mdl.predict_proba(tr[F])[:, 1]
    print(f"\n{label}: AUC {roc_auc_score(te.y, mdl.predict_proba(te[F])[:, 1]):.3f} (0.5 = random)")
    for m in ("spot", "futures"):
        full, X, T = data[m]
        base = portfolio(m, full, T, start, end, max_positions=8)
        ml = portfolio(m, full, T, start, end, max_positions=8,
                       risk_scale=scale(m, mdl, np.quantile(pt, 0.3), np.quantile(pt, 0.7)))
        print(f"  {m:7s} without ML: {fmt(base)}")
        print(f"  {m:7s} ML sizing : {fmt(ml)}")
