"""Can a faster (more trades) setup beat the 4h bot? 1h / 15m candles, fees included,
judged on 2021-24 (train) AND 2025-26 (holdout), same shared-account engine as the live bot.

Usage: python research/faster.py spot futures      (downloads 1h/15m history on first run)

Result (Oct 2026, 8 coins, risk 0.5%): faster = worse. Holdout 2025-26, best case per timeframe:
  spot     4h EMA trend  +9.5%/yr  DD -14%   ~15 trades/month   <- current bot
  spot     1h EMA trend -15.2%/yr  DD -43%   ~64 trades/month
  spot    15m EMA trend -76.9%/yr  DD -94%  ~189 trades/month
  futures  1h / 15m: -19% / -88% per year. Mean-reversion (RSI, Connors RSI(2)) loses on every timeframe.
Without any fees 1h/15m would be profitable: the edge per trade is smaller than the round-trip cost.
"""
import sys
import time
import warnings

import numpy as np
import pandas as pd

from _common import BASES, END, SPLIT, params
from tradingbot.backtest import run_portfolio
from tradingbot.config import Settings
from tradingbot.data import _cache_path, load_history
from tradingbot.indicators import ema, rsi
from tradingbot.strategies import apply_btc_filter, donchian_breakout, ema_trend, rsi_reversion

warnings.filterwarnings("ignore")
HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0}


def load(market, base, tf):
    s = Settings(market=market, symbols=[f"{base}/USDT"]).validate()
    p = _cache_path(market, s.symbols[0], tf)
    if p.exists():
        d = pd.read_csv(p, index_col=0, parse_dates=True)
        if d.index.tz is None:
            d.index = d.index.tz_localize("UTC")
        return d[d.index >= "2020-10-01"]
    return load_history(s, s.symbols[0], tf, "2020-10-01")


def rsi2(df, short):
    """Connors RSI(2): buy a sharp dip inside an uptrend, sell when price closes above EMA(5)."""
    c = df["close"].to_numpy()
    r, t, e5 = rsi(df["close"], 2).to_numpy(), ema(df["close"], 200).to_numpy(), ema(df["close"], 5).to_numpy()
    out, pos = np.zeros(len(c), dtype=int), 0
    for i in range(len(c)):
        if np.isnan(t[i]) or np.isnan(r[i]):
            continue
        if pos == 1 and c[i] > e5[i]:
            pos = 0
        elif pos == -1 and c[i] < e5[i]:
            pos = 0
        if pos == 0:
            if r[i] < 10 and c[i] > t[i]:
                pos = 1
            elif short and r[i] > 90 and c[i] < t[i]:
                pos = -1
        out[i] = pos
    return pd.Series(out, index=df.index)


CASES = [  # name, signal fn, atr stop
    ("EMA trend 20/50/200", lambda d, s: ema_trend(d, s), 5.0),
    ("EMA trend 20/50/200, 3xATR", lambda d, s: ema_trend(d, s), 3.0),
    ("Donchian 20/10 breakout", lambda d, s: donchian_breakout(d, s), 3.0),
    ("RSI(14) dip-buy", lambda d, s: rsi_reversion(d, s), 3.0),
    ("Connors RSI(2)", rsi2, 3.0),
]


def run(market, full, tg, tf, a, b, atr, fee=None):
    dfs = {k: d[(d.index >= a) & (d.index < b)] for k, d in full.items()}
    dfs = {k: d for k, d in dfs.items() if len(d) > 300}
    p = params(market, atr_mult=atr, trailing=False, risk_per_trade=0.005, bar_hours=HOURS[tf])
    if fee is not None:
        p.fee = fee
    r = run_portfolio(dfs, {k: tg[k].loc[dfs[k].index] for k in dfs}, p, max_positions=6)
    months = (pd.Timestamp(b) - pd.Timestamp(a)).days / 30.4
    return r, r.metrics["trades"] / months


def fmt(r, per_month):
    m = r.metrics
    return (f"yıllık {m['cagr_%']:6.1f}%  DD {m['max_drawdown_%']:6.1f}%  Sharpe {m['sharpe']:5.2f}  "
            f"isabet {m['win_rate_%']:4.1f}%  işlem/ay {per_month:5.1f}")


def main(markets):
    A, S, E = (pd.Timestamp(x, tz="UTC") for x in ("2021-01-01", SPLIT, END))
    for m in markets:
        short = m == "futures"
        for tf in ("4h", "1h", "15m"):
            t0 = time.time()
            full = {b: load(m, b, tf) for b in BASES}
            btc = full["BTC"]
            for name, fn, atr in CASES:
                tg = {k: apply_btc_filter(fn(d, short), btc) for k, d in full.items()}
                tr, trm = run(m, full, tg, tf, A, S, atr)
                ho, hom = run(m, full, tg, tf, S, E, atr)
                print(f"{m:7s} {tf:3s} {name:28s} EĞİTİM {fmt(tr, trm)} || SAKLI {fmt(ho, hom)}", flush=True)
            print(f"   ({tf} {m}: {time.time() - t0:.0f} sn)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ["spot"])
