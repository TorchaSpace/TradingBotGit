"""Freqtrade-style StoplossGuard on the trend bot: pause new entries after N stops within a window.
Result (Oct 2026): no effect at all on spot, +-0.1% on futures (the 5xATR stops rarely cluster and
re-entries are already blocked until the signal resets). Kept as a backtest option, not used live.
Usage: python research/stoploss_guard.py   (needs the 4h cache: python -m tradingbot portfolio once)"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd
from dataclasses import replace
from tradingbot.config import Settings
from tradingbot.validation import _params, _portfolio, HOLDOUT_START
from tradingbot.strategies import make_target
from tradingbot.data import CACHE_DIR


def load(m, sym):
    d = pd.read_csv(CACHE_DIR / f"{m}_{sym.replace('/', '').replace(':', '_')}_4h.csv", index_col=0, parse_dates=True)
    return d[d.index >= "2020-01-01"]
for m in ("spot", "futures"):
    s = Settings(market=m).validate(); full = {x: load(m, x) for x in s.symbols}; btc = load(m, s.btc_symbol)
    tg = {k: make_target(s.strategy, d, s.allow_short, btc) for k, d in full.items()}
    for n, w, pz in ((0, 24, 24), (3, 24, 24), (3, 24, 48), (4, 24, 24), (4, 48, 48), (5, 48, 48), (2, 12, 24)):
        p = replace(_params(s), stop_guard_n=n, stop_guard_window_h=w, stop_guard_pause_h=pz)
        a = _portfolio(full, tg, p, 6, "2021-01-01", HOLDOUT_START).metrics; b = _portfolio(full, tg, p, 6, HOLDOUT_START).metrics
        print(f"{m:7s} guard n={n} pencere={w}s mola={pz}s | EĞİTİM %{a['cagr_%']:5.1f} DD {a['max_drawdown_%']:6.1f} Sh {a['sharpe']:.2f} | SAKLI %{b['cagr_%']:5.1f} DD {b['max_drawdown_%']:6.1f} Sh {b['sharpe']:.2f}", flush=True)
