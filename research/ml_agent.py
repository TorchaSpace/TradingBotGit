"""Walk-forward test of the ML agent (src/tradingbot/mlagent.py) on hourly data since 2017.

Every quarter the model is retrained on data that ended before the quarter (24h embargo), then it
scores that quarter. The paper rule (buy if p >= 0.65, sell 24h later, 1/N of equity per coin,
0.3% round-trip cost) is simulated per year. Usage: python research/ml_agent.py   (downloads 1h data)

Result (Oct 2026, 8 coins, entry at the next hour's open): 2020 -3%, 2021 +234%, 2022 -20%, 2023 +16%,
2024 +53%, 2025 +39%, 2026 YTD +2%; worst in-year drop -40% (2020-22), under -10% since 2023.
Same picture for 12h / 48h horizons and thresholds 0.6-0.7. Buy & hold 2025-26: -19%/yr, DD -68%.
"""
import warnings

import numpy as np
import pandas as pd

from tradingbot import mlagent as M
from tradingbot.config import Settings

warnings.filterwarnings("ignore")

if __name__ == "__main__":
    s = Settings().validate()
    data, btc = M.load_training_data(s, progress=print)
    X = M.features(data, btc, with_label=True)
    X["y"] = (X["fwd"] > M.COST).astype(int)
    preds = []
    qs = pd.date_range("2020-01-01", pd.Timestamp.now(tz="UTC") + pd.offsets.QuarterBegin(), freq="QS", tz="UTC")
    for q0, q1 in zip(qs[:-1], qs[1:]):
        tr = X[X.index < q0 - pd.Timedelta(hours=M.HORIZON)].dropna(subset=["fwd"]).iloc[::3]
        te = X[(X.index >= q0) & (X.index < q1)].dropna(subset=["fwd"])
        if len(te):
            m = M._model().fit(tr[M.FEATURES], tr["y"])
            preds.append(pd.DataFrame({"p": m.predict_proba(te[M.FEATURES])[:, 1], "fwd": te["fwd"], "coin": te["coin"]},
                                      index=te.index))
            print(q0.date(), "done", flush=True)
    P = pd.concat(preds)
    for yr, g in P.groupby(P.index.year):
        r = M._paper_check(g)
        print(yr, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})
    print("ortalama yıllık değişim (al-tut):",
          {yr: round(float(np.mean([d['close'][d.index.year == yr].iloc[-1] / d['close'][d.index.year == yr].iloc[0] - 1
                                   for d in data.values() if (d.index.year == yr).sum() > 100])) * 100, 1)
           for yr in sorted(set(P.index.year))})
