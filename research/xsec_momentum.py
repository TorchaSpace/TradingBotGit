"""Cross-sectional momentum: every week hold the strongest coins of the last month.

    python research/xsec_momentum.py

Result (2026-10): big returns in 2021-2024 but drawdowns of -55% to -70% (no stops, always fully
invested), and the 2025-26 holdout swings from -10% to +36% depending on small parameter
changes -> fragile, against the goal of keeping losses small. Not used.
"""
import pandas as pd

from _common import END, SPLIT, WIDE, universe
from tradingbot.backtest import compute_metrics
from tradingbot.indicators import ema

full = universe("spot", WIDE)
P = pd.DataFrame({b: d.close.resample("D").last() for b, d in full.items()}).sort_index()
regime = (P["BTC"] > ema(P["BTC"], 100)).astype(float)


def sim(lookback=30, top=4, every=7, cost=0.0025, start="2021-01-01", end=END):
    R = P.pct_change(fill_method=None)
    mom = P.shift(1) / P.shift(lookback + 1) - 1
    days = P.index[(P.index >= pd.Timestamp(start, tz="UTC")) & (P.index < pd.Timestamp(end, tz="UTC"))]
    w = pd.Series(0.0, index=P.columns)
    out = []
    for k, t in enumerate(days):
        r = (w * R.loc[t].fillna(0)).sum()
        if k % every == 0:
            nw = pd.Series(0.0, index=P.columns)
            m = mom.loc[t].dropna()
            if regime.loc[t] > 0:
                nw[m[m > 0].sort_values(ascending=False).index[:top]] = 1 / top
            r -= cost * (nw - w).abs().sum()
            w = nw
        out.append(r)
    E = (1 + pd.Series(out, index=days)).cumprod() * 1000
    return compute_metrics(E, pd.DataFrame(), 24)


for lb, top, every in [(30, 4, 7), (60, 4, 7), (14, 4, 7), (30, 8, 7), (30, 4, 14)]:
    a, b = sim(lb, top, every, end=SPLIT), sim(lb, top, every, start=SPLIT)
    print(f"lookback={lb} top={top} every={every}d | 2021-24: CAGR {a['cagr_%']:6.1f}% DD {a['max_drawdown_%']:6.1f}%"
          f" || 2025-26: CAGR {b['cagr_%']:6.1f}% DD {b['max_drawdown_%']:6.1f}%")
