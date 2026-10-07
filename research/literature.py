"""Literature ideas vs the current bot, judged on 2021-24 (train) AND 2025-26 (holdout).

1. Time-series momentum, TSMOM (Moskowitz, Ooi & Pedersen 2012): sign of the 30/90/180-day return, voted.
2. Volatility-managed risk (Moreira & Muir 2017): risk per trade x (long-run BTC vol / recent BTC vol).
   Compared against a FLAT risk with the same average size, otherwise "more risk" looks like "better timing".

Usage: python research/literature.py spot futures
Result (Oct 2026): neither beat the current EMA trend + BTC filter on both periods -> not adopted.
"""
import sys
import warnings

import numpy as np
import pandas as pd

from _common import BASES, END, SPLIT, fmt, portfolio, universe
from tradingbot.strategies import apply_btc_filter, ema_trend

warnings.filterwarnings("ignore")


def tsmom(df: pd.DataFrame, short: bool, lookbacks=(180, 540, 1080), threshold: float = 0.0) -> pd.Series:
    """Multi-horizon vote on 4h bars (180 bars = 30 days)."""
    c = df["close"]
    score = sum(np.sign(c / c.shift(n) - 1) for n in lookbacks) / len(lookbacks)
    out = pd.Series(0, index=df.index)
    out[score > threshold] = 1
    if short:
        out[score < -threshold] = -1
    out[c.shift(max(lookbacks)).isna()] = 0
    return out.astype(int)


def vol_scale(btc: pd.DataFrame, window: int = 180, lo: float = 0.5, hi: float = 1.5) -> pd.Series:
    """Known at bar close: expanding median of rolling vol / current rolling vol, clipped."""
    r = np.log(btc["close"]).diff()
    rv = r.rolling(window).std()
    return (rv.expanding(500).median() / rv).clip(lo, hi).fillna(1.0)


def main(markets):
    for m in markets:
        full = universe(m, BASES)
        short, btc = m == "futures", full["BTC"]
        base = {b: apply_btc_filter(ema_trend(d, short), btc) for b, d in full.items()}
        ts = {b: apply_btc_filter(tsmom(d, short), btc) for b, d in full.items()}
        ts2 = {b: apply_btc_filter(tsmom(d, short, threshold=0.34), btc) for b, d in full.items()}

        def both(name, tg, **kw):
            a = portfolio(m, full, tg, "2021-01-01", SPLIT, **kw)
            b = portfolio(m, full, tg, SPLIT, END, **kw)
            print(f"{m:7s} {name:44s} TRAIN {fmt(a)} || HOLDOUT {fmt(b)}", flush=True)

        both("EMA trend + BTC filter (current bot)", base)
        both("TSMOM 30/90/180d vote", ts)
        both("TSMOM, 2 of 3 horizons agree", ts2)
        for window, lo, hi in ((180, 0.5, 1.5), (180, 0.5, 1.0), (360, 0.5, 1.5), (84, 0.5, 1.5)):
            vs = vol_scale(btc, window, lo, hi)
            scale = {b: vs.reindex(d.index).ffill().fillna(1.0) for b, d in full.items()}
            avg_tr, avg_ho = vs.loc["2021-01-01":SPLIT].mean(), vs.loc[SPLIT:].mean()
            name = f"vol-managed {window // 6}d [{lo},{hi}]"
            a = portfolio(m, full, base, "2021-01-01", SPLIT, risk_scale=scale)
            b = portfolio(m, full, base, SPLIT, END, risk_scale=scale)
            a0 = portfolio(m, full, base, "2021-01-01", SPLIT, risk_per_trade=0.005 * avg_tr)
            b0 = portfolio(m, full, base, SPLIT, END, risk_per_trade=0.005 * avg_ho)
            print(f"{m:7s} {name:44s} timed: TRAIN {fmt(a)} || HOLDOUT {fmt(b)}\n"
                  f"{'':7s} {'  same average risk, no timing':44s} flat : TRAIN {fmt(a0)} || HOLDOUT {fmt(b0)}",
                  flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ["spot"])
