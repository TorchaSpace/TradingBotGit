"""Can a machine-learning model predict the next 2 days (12 x 4h bars) of price direction?

Gradient boosting + logistic regression on 15 technical features, 8 coins pooled,
walk-forward (train on everything before each 6-month window, test on that window).
Needs: pip install scikit-learn

    python research/ml_experiment.py spot
Result (2026-10): AUC 0.515, accuracy 50.9% (0.50 = coin flip). As a stand-alone strategy it LOST money after fees
(-42%); as an entry filter on ema_trend it raised the win rate but did not improve Sharpe.
-> not used in the live bot.
"""
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from _common import BASES, load
from tradingbot.indicators import adx, atr, ema, higher_tf_trend, rsi

market = sys.argv[1] if len(sys.argv) > 1 else "spot"
H = 12


def feats(df):
    c, a = df.close, atr(df)
    X = pd.DataFrame(index=df.index)
    for k in (1, 3, 6, 12, 24, 48, 96):
        X[f"r{k}"] = np.log(c / c.shift(k))
    X["rsi"], X["atrp"] = rsi(c), a / c
    for n in (20, 50, 200):
        X[f"d{n}"] = (c - ema(c, n)) / a
    X["adx"] = adx(df)
    X["vz"] = (df.volume - df.volume.rolling(120).mean()) / df.volume.rolling(120).std()
    X["htf"] = higher_tf_trend(df)
    X["vol_ratio"] = c.pct_change().rolling(24).std() / c.pct_change().rolling(240).std()
    return X


rows = []
for b in BASES:
    df = load(market, b)
    X = feats(df)
    fwd = np.log(df.close.shift(-H) / df.close)
    X["y"], X["fwd"] = (fwd > 0).astype(float).where(fwd.notna()), fwd
    rows.append(X)
D = pd.concat(rows).dropna()
F = [c for c in D.columns if c not in ("y", "fwd")]
preds = []
for t0 in pd.date_range("2021-01-01", D.index.max().tz_convert(None), freq="6MS", tz="UTC"):
    t1 = t0 + pd.DateOffset(months=6)
    tr = D[D.index < t0 - pd.Timedelta(hours=4 * H)]
    te = D[(D.index >= t0) & (D.index < t1)]
    if not len(te):
        break
    m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                       min_samples_leaf=200, l2_regularization=1.0).fit(tr[F], tr.y)
    preds.append(pd.DataFrame({"p": m.predict_proba(te[F])[:, 1], "y": te.y.values,
                               "fwd": te.fwd.values}, index=te.index))
P = pd.concat(preds)
hi = P[P.p > 0.55]
print(f"{market}: out-of-sample AUC = {roc_auc_score(P.y, P.p):.3f} (0.5 = coin flip), "
      f"accuracy = {((P.p > 0.5) == (P.y == 1)).mean():.3f}, base rate = {P.y.mean():.3f}")
print(f"when model is confident (p>0.55): n={len(hi)}, correct {hi.y.mean():.1%}, "
      f"avg 2-day move {hi.fwd.mean() * 100:+.2f}% vs all {P.fwd.mean() * 100:+.2f}% "
      f"(round-trip spot fee+slippage ~0.3%)")
