"""FreqAI-style outlier filter (skip predictions far from the training data) and triple-barrier labels
(Lopez de Prado: +/-1.5 daily sigma or 24h, whichever comes first) vs the current ML agent.
Model trained on 32 coins, trades the 8 core coins, quarterly walk-forward, costs 0.3% round trip.

Result (Oct 2026), yearly return 2020..2026:
  current agent      +107 +286 -13 +36 +49 +31 +7
  + outlier filter   + 85 +204 -21 +36 +39 +22 +6   -> worse every year, not adopted
  triple barrier     + 46 + 91  -3 +28 +20  +2 -10  -> better only in 2022, not adopted
Usage: python research/ml_outlier_triple_barrier.py
"""
import time
import warnings

warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
import tradingbot.mlagent as M
CORE = [f"{b}/USDT" for b in ["BTC","ETH","SOL","BNB","XRP","ADA","LINK","DOGE"]]
from tradingbot.config import Settings
data, _ = M.load_training_data(Settings().validate(), progress=print)
X = M.features(data, data["BTC/USDT"], with_label=True)
# triple barrier label: +/- 1.5 daily sigma within 24h, using hourly highs/lows
rows = []
for sym, d in data.items():
    c, h, l = d["close"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy()
    sig = (np.log(d["close"]).diff().rolling(24).std() * np.sqrt(24)).to_numpy()
    n = len(c); lab = np.full(n, np.nan); ret = np.full(n, np.nan)
    for i in range(n - 25):
        if np.isnan(sig[i]): continue
        up, dn = c[i] * (1 + 1.5 * sig[i]), c[i] * (1 - 1.5 * sig[i])
        out = None
        for j in range(i + 1, i + 25):
            if l[j] <= dn: out = (0, dn / c[i] - 1); break            # stop first (conservative if both)
            if h[j] >= up: out = (1, up / c[i] - 1); break
        if out is None: out = (int(c[i + 24] / c[i] - 1 > M.COST), c[i + 24] / c[i] - 1)
        lab[i], ret[i] = out
    rows.append(pd.DataFrame({"coin": sym, "tb_y": lab, "tb_ret": ret}, index=d.index))
TB = pd.concat(rows).reset_index(names="t")
X = X.reset_index(names="t").merge(TB, on=["t", "coin"], how="left").set_index("t").sort_index()
X["y"] = (X["fwd"] > M.COST).astype(int)
X = X.dropna(subset=["fwd", "tb_y"])
print("örnek", len(X), flush=True)
def sim(P, retcol):
    tr = []
    for coin, g in P[P.p >= 0.65].groupby("coin"):
        free = None
        for t, r in g.iterrows():
            if free is not None and t < free: continue
            tr.append((t + pd.Timedelta(hours=24), r[retcol] - M.COST)); free = t + pd.Timedelta(hours=24)
    T = pd.DataFrame(tr, columns=["exit", "ret"]).set_index("exit").sort_index()
    daily = (T.ret / 8).resample("D").sum()
    out = {}
    for yr, g in daily.groupby(daily.index.year):
        eq = (1 + g).cumprod(); out[yr] = (round((eq.iloc[-1] - 1) * 100, 1), round(((eq / eq.cummax()).min() - 1) * 100, 1),
                                           int((T.index.year == yr).sum()), round((T[T.index.year == yr].ret > 0).mean() * 100))
    return out
base, outl, trip = [], [], []
qs = pd.date_range("2020-01-01", "2026-10-01", freq="QS", tz="UTC"); t0 = time.time()
for q0, q1 in zip(qs[:-1], qs[1:]):
    tr = X[X.index < q0 - pd.Timedelta(hours=24)].iloc[::3]
    te = X[(X.index >= q0) & (X.index < q1)]
    te = te[te.coin.isin(CORE)]
    m1 = M._model().fit(tr[M.FEATURES], tr["y"]); p1 = m1.predict_proba(te[M.FEATURES])[:, 1]
    mu, sd = tr[M.FEATURES].mean(), tr[M.FEATURES].std() + 1e-9
    di_tr = ((tr[M.FEATURES] - mu) / sd).abs().mean(1); thr = di_tr.quantile(0.99)
    di = ((te[M.FEATURES] - mu) / sd).abs().mean(1).to_numpy()
    m2 = M._model().fit(tr[M.FEATURES], tr["tb_y"].astype(int)); p2 = m2.predict_proba(te[M.FEATURES])[:, 1]
    fwdret = np.expm1(te["fwd"]).to_numpy()
    base.append(pd.DataFrame({"p": p1, "r": fwdret, "coin": te.coin}, index=te.index))
    outl.append(pd.DataFrame({"p": np.where(di > thr, 0, p1), "r": fwdret, "coin": te.coin}, index=te.index))
    trip.append(pd.DataFrame({"p": p2, "r": te["tb_ret"].to_numpy(), "coin": te.coin}, index=te.index))
print("süre", round(time.time() - t0), flush=True)
for name, L in (("şu anki ML ajan", base), ("+ aykırı durum filtresi", outl), ("üçlü bariyer etiket+çıkış", trip)):
    print(name, sim(pd.concat(L), "r"), flush=True)
