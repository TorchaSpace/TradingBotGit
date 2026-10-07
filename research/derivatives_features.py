"""Do derivatives features help the ML agent? funding rate (level, 3-day mean, z-score), perp-spot
basis (level, 24h change, z-score) and perp/spot volume ratio, added to the price/volume features.
Same 8 coins, same rows, quarterly walk-forward retraining on past data only, 2022 -> today.
(Open interest and long/short ratios are not tested: Binance only serves their last 30 days.)

Result (Oct 2026): AUC 0.526 -> 0.534 (2022-24) and 0.514 -> 0.517 (2025-26), but the trading
result is not consistently better (2022 -27% -> -33%, 2024 +54% -> +77%, 2025 +57% -> +31%).
Not adopted. Usage: python research/derivatives_features.py
"""
import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from tradingbot import mlagent as M
from tradingbot.config import Settings
from tradingbot.data import load_funding, load_history, load_hourly_archive

warnings.filterwarnings("ignore")
BASES = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "LINK", "DOGE"]
DERIV = ["basis", "basis_chg24", "basis_z", "funding", "funding_3d", "funding_z", "perp_vol_ratio"]

if __name__ == "__main__":
    spot_s, fut_s = Settings(market="spot").validate(), Settings(market="futures").validate()
    data = {}
    for b in BASES:
        load_hourly_archive(f"{b}/USDT", "2020-01-01")
        data[f"{b}/USDT"] = load_history(spot_s, f"{b}/USDT", "1h", "2020-01-01")
    X = M.features(data, data["BTC/USDT"], with_label=True)
    extra = []
    for b in BASES:
        spot = data[f"{b}/USDT"]
        fut = load_history(fut_s, f"{b}/USDT:USDT", "1h", "2020-10-01")
        fund = load_funding(b)
        basis = fut["close"] / spot["close"].reindex(fut.index) - 1
        fr = fund.reindex(fut.index, method="ffill")
        f = pd.DataFrame({
            "basis": basis, "basis_chg24": basis - basis.shift(24),
            "basis_z": (basis - basis.rolling(168).mean()) / (basis.rolling(168).std() + 1e-9),
            "funding": fr, "funding_3d": fund.rolling(9).mean().reindex(fut.index, method="ffill"),
            "funding_z": (fr - fund.rolling(90).mean().reindex(fut.index, method="ffill"))
                         / (fund.rolling(90).std().reindex(fut.index, method="ffill") + 1e-9),
            "perp_vol_ratio": np.log((fut["volume"] * fut["close"]).rolling(24).sum()
                                     / (spot["volume"] * spot["close"]).reindex(fut.index).rolling(24).sum()),
        }, index=fut.index)
        f["coin"] = f"{b}/USDT"
        extra.append(f.reset_index(names="t"))
    X = X.reset_index(names="t").merge(pd.concat(extra), on=["t", "coin"]).set_index("t").sort_index()
    X = X.dropna(subset=DERIV + ["fwd"])
    X = X[X.index >= "2021-01-01"]
    X["y"] = (X["fwd"] > M.COST).astype(int)
    for name, feats in (("price/volume", M.FEATURES), ("+ derivatives", M.FEATURES + DERIV)):
        preds = []
        qs = pd.date_range("2022-01-01", pd.Timestamp.now(tz="UTC") + pd.offsets.QuarterBegin(), freq="QS", tz="UTC")
        for q0, q1 in zip(qs[:-1], qs[1:]):
            tr = X[X.index < q0 - pd.Timedelta(hours=24)].iloc[::3]
            te = X[(X.index >= q0) & (X.index < q1)]
            if len(te):
                m = M._model().fit(tr[feats], tr["y"])
                preds.append(pd.DataFrame({"p": m.predict_proba(te[feats])[:, 1], "fwd": te["fwd"],
                                           "coin": te["coin"], "y": te["y"]}, index=te.index))
        P = pd.concat(preds)
        print(name, "AUC", round(roc_auc_score(P["y"], P["p"]), 3))
        for yr, g in P.groupby(P.index.year):
            r = M._paper_check(g)
            print(f"   {yr}: {r['return_pct']:+.1f}%  DD {r['max_dd_pct']:.1f}%  trades {r['trades']}")
