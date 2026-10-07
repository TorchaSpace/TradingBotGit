"""Day-trading ideas from the literature, flat at the end of every UTC day, fees included.

1. Volatility breakout (L. Williams): buy when price exceeds today's open + k x yesterday's range.
2. Intraday time-series momentum (Gao, Han, Li & Zhou 2018; for Bitcoin: Wen et al. 2022):
   at 20:00 UTC trade in the direction of the day's return so far, exit at the day's close.
3. Opening-range breakout (first 2 hours of the UTC day), stop on the other side of the range.
Judged on 2021-24 (train) and 2025-26 (holdout). Usage: python research/daytrade.py
"""
import sys
import warnings

import numpy as np
import pandas as pd

from faster import BASES, END, HOURS, SPLIT, load, params
from tradingbot.backtest import run_portfolio
from tradingbot.indicators import ema

warnings.filterwarnings("ignore")


def _day_last(idx):
    d = idx.floor("D")
    return pd.Series(d, index=idx).shift(-1).ne(pd.Series(d, index=idx)).to_numpy()


def vol_breakout(df, short, k=0.5, trend=None):
    day = df.index.floor("D")
    g = df.groupby(day)
    dopen = g["open"].transform("first")
    dh, dl = g["high"].max(), g["low"].min()
    prev_range = (dh - dl).shift(1).reindex(day).to_numpy()
    c = df["close"].to_numpy()
    up, dn = dopen.to_numpy() + k * prev_range, dopen.to_numpy() - k * prev_range
    last = _day_last(df.index)
    tr = None if trend is None else trend.reindex(df.index, method="ffill").to_numpy()
    out, pos, cur = np.zeros(len(c), dtype=int), 0, None
    for i in range(len(c)):
        if day[i] != cur:
            cur, pos = day[i], 0
        if pos == 0 and not np.isnan(up[i]):
            if c[i] > up[i] and (tr is None or tr[i] > 0):
                pos = 1
            elif short and c[i] < dn[i] and (tr is None or tr[i] < 0):
                pos = -1
        out[i] = 0 if last[i] else pos
    return pd.Series(out, index=df.index)


def intraday_momentum(df, short, hour=20):
    day = df.index.floor("D")
    dopen = df.groupby(day)["open"].transform("first").to_numpy()
    c, h = df["close"].to_numpy(), df.index.hour
    last = _day_last(df.index)
    out, pos, cur = np.zeros(len(c), dtype=int), 0, None
    for i in range(len(c)):
        if day[i] != cur:
            cur, pos = day[i], 0
        if h[i] == hour - 1 and pos == 0:          # decided at the close of the 19:00 bar
            r = c[i] / dopen[i] - 1
            pos = 1 if r > 0 else (-1 if short and r < 0 else 0)
        out[i] = 0 if last[i] else pos
    return pd.Series(out, index=df.index)


def opening_range(df, short, bars=8):
    day = df.index.floor("D")
    n = df.groupby(day).cumcount().to_numpy()
    hi = df.groupby(day)["high"].transform(lambda x: x.iloc[:bars].max()).to_numpy()
    lo = df.groupby(day)["low"].transform(lambda x: x.iloc[:bars].min()).to_numpy()
    c = df["close"].to_numpy()
    last = _day_last(df.index)
    out, pos, cur = np.zeros(len(c), dtype=int), 0, None
    for i in range(len(c)):
        if day[i] != cur:
            cur, pos = day[i], 0
        if n[i] >= bars and pos == 0:
            if c[i] > hi[i]:
                pos = 1
            elif short and c[i] < lo[i]:
                pos = -1
        elif pos == 1 and c[i] < lo[i]:
            pos = 0
        elif pos == -1 and c[i] > hi[i]:
            pos = 0
        out[i] = 0 if last[i] else pos
    return pd.Series(out, index=df.index)


def run(m, full, tg, tf, a, b, fee, atr=3.0):
    dfs = {k: d[(d.index >= a) & (d.index < b)] for k, d in full.items()}
    p = params(m, atr_mult=atr, trailing=False, risk_per_trade=0.005, bar_hours=HOURS[tf])
    p.fee = fee
    r = run_portfolio(dfs, {k: tg[k].loc[dfs[k].index] for k in dfs}, p, max_positions=6)
    return r, r.metrics["trades"] / ((b - a).days / 30.4)


def fmt(r, pm):
    m = r.metrics
    return f"yıllık {m['cagr_%']:6.1f}% DD {m['max_drawdown_%']:6.1f}% Sh {m['sharpe']:5.2f} isabet {m['win_rate_%']:4.1f}% işlem/ay {pm:5.1f}"


if __name__ == "__main__":
    A, S, E = (pd.Timestamp(x, tz="UTC") for x in ("2021-01-01", SPLIT, END))
    for m in sys.argv[1:] or ["spot", "futures"]:
        short = m == "futures"
        fees = [("taker", 0.001 if m == "spot" else 0.0005), ("maker", 0.00075 if m == "spot" else 0.0002)]
        h1 = {b: load(m, b, "1h") for b in BASES}
        m15 = {b: load(m, b, "15m") for b in BASES}
        daily_trend = {b: np.sign(d["close"].resample("D").last().pipe(lambda s: s - ema(s, 50)).shift(1))
                       for b, d in h1.items()}
        cases = [
            ("Volatilite kırılımı k=0.5 (1h)", h1, "1h", {b: vol_breakout(d, short) for b, d in h1.items()}),
            ("Volatilite kırılımı + günlük trend", h1, "1h", {b: vol_breakout(d, short, trend=daily_trend[b]) for b, d in h1.items()}),
            ("Gün içi momentum 20:00 UTC (1h)", h1, "1h", {b: intraday_momentum(d, short) for b, d in h1.items()}),
            ("Açılış aralığı kırılımı 2s (15m)", m15, "15m", {b: opening_range(d, short) for b, d in m15.items()}),
        ]
        for name, full, tf, tg in cases:
            for fl, fee in fees:
                tr, a1 = run(m, full, tg, tf, A, S, fee)
                ho, a2 = run(m, full, tg, tf, S, E, fee)
                print(f"{m:7s} {name:36s} {fl:5s} EĞİTİM {fmt(tr, a1)} || SAKLI {fmt(ho, a2)}", flush=True)
