"""Does running several parameter sets / strategies side by side beat the single default?

Each variant gets an equal slice of capital (equal-weighted sub-portfolios), 8 coins, BTC filter on.
Measured on 2021-2024 and on the untouched 2025-26 holdout.

    python research/ensemble.py spot futures

Result (2026-10): the EMA ensemble and EMA+Donchian were worse on the holdout (spot Sharpe 0.70 /
0.71 vs 0.74); the stop ensemble was marginally better (+0.01-0.02 Sharpe, futures holdout slightly
worse) - within noise. The current setup already sits in a flat, robust region -> kept as is.
"""
import sys

import pandas as pd

from _common import BASES, END, SPLIT, portfolio, universe
from tradingbot.backtest import compute_metrics
from tradingbot.strategies import apply_btc_filter, donchian_breakout, ema_trend


def combine(curves):
    r = pd.concat([c.pct_change().fillna(0) for c in curves], axis=1).fillna(0).mean(axis=1)
    return (1 + r).cumprod() * 1000


def fmt(e):
    m = compute_metrics(e, pd.DataFrame(), 4)
    return f"CAGR {m['cagr_%']:6.1f}%  DD {m['max_drawdown_%']:6.1f}%  Sharpe {m['sharpe']:5.2f}"


VARIANTS = {
    "single EMA 20/50, 5xATR (bot)": [(ema_trend, {}, 5)],
    "EMA ensemble 10/30 + 20/50 + 30/100": [(ema_trend, dict(fast=10, slow=30), 5), (ema_trend, {}, 5),
                                            (ema_trend, dict(fast=30, slow=100), 5)],
    "stop ensemble 4/5/6 x ATR": [(ema_trend, {}, 4), (ema_trend, {}, 5), (ema_trend, {}, 6)],
    "EMA + Donchian 20/10": [(ema_trend, {}, 5), (donchian_breakout, {}, 5)],
}
for m in sys.argv[1:] or ["spot"]:
    full = universe(m, BASES)
    short = m == "futures"
    for name, parts in VARIANTS.items():
        res = []
        for a, b in (("2021-01-01", SPLIT), (SPLIT, END)):
            curves = []
            for fn, kw, atr_mult in parts:
                tg = {k: apply_btc_filter(fn(d, short, **kw), full["BTC"]) for k, d in full.items()}
                curves.append(portfolio(m, full, tg, a, b, atr_mult=atr_mult).equity)
            res.append(fmt(combine(curves)))
        print(f"{m:7s} {name:36s} 2021-24: {res[0]} || 2025-26: {res[1]}", flush=True)
