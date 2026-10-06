"""Walk-forward test: does re-optimising parameters every 6 months beat a fixed robust setup?

For each 6-month window from 2021: pick the config with the best Sharpe on the previous 2 years,
then trade it on the next (unseen) 6 months. Compare the stitched result with the fixed default.

    python research/walk_forward.py spot
Result (2026-10): re-optimising did WORSE than the fixed setup (spot Sharpe 1.15 vs 1.49,
maxDD -24% vs -16%; futures 1.04 vs 1.20) -> classic overfitting; the bot uses the fixed setup.
"""
import sys

import pandas as pd

from _common import BASES, load, params
from tradingbot.backtest import compute_metrics, run_portfolio
from tradingbot.strategies import donchian_breakout, ema_trend

market = sys.argv[1] if len(sys.argv) > 1 else "spot"
short = market == "futures"
full = {b: load(market, b) for b in BASES}
SIG, cfgs = {}, []
for f, s in [(10, 30), (20, 50), (30, 100)]:
    for b in BASES:
        SIG[("ema", f, s, b)] = ema_trend(full[b], short, fast=f, slow=s)
    cfgs += [("ema", f, s, am, tr) for am in (3, 4, 5, 6) for tr in (False, True)]
for e, x in [(20, 10), (55, 20)]:
    for b in BASES:
        SIG[("don", e, x, b)] = donchian_breakout(full[b], short, entry=e, exit_=x)
    cfgs += [("don", e, x, am, tr) for am in (3, 4, 5, 6) for tr in (False, True)]
DEFAULT = ("ema", 20, 50, 5, False)


def run(cfg, start, end):
    k, a1, a2, am, tr = cfg
    dfs = {b: d[(d.index >= start) & (d.index < end)] for b, d in full.items()}
    dfs = {b: d for b, d in dfs.items() if len(d) > 50}
    tg = {b: SIG[(k, a1, a2, b)].loc[dfs[b].index] for b in dfs}
    return run_portfolio(dfs, tg, params(market, atr_mult=am, trailing=tr, risk_per_trade=0.005),
                         max_positions=6)


end_all = max(d.index[-1] for d in full.values())
oos, fixed = [], []
for t0 in pd.date_range("2021-01-01", end_all.tz_convert(None), freq="6MS", tz="UTC"):
    t1 = min(t0 + pd.DateOffset(months=6), end_all)
    best = max(cfgs, key=lambda c: run(c, t0 - pd.DateOffset(years=2), t0).metrics["sharpe"])
    r, rf = run(best, t0, t1), run(DEFAULT, t0, t1)
    oos.append(r.equity.pct_change().fillna(0))
    fixed.append(rf.equity.pct_change().fillna(0))
    print(f"{t0.date()} picked {best}: {r.metrics['total_return_%']:+.1f}%  | fixed: {rf.metrics['total_return_%']:+.1f}%")
for name, parts in (("WALK-FORWARD (re-optimised)", oos), ("FIXED DEFAULT", fixed)):
    E = (1 + pd.concat(parts)).cumprod() * 1000
    m = compute_metrics(E, pd.DataFrame(), 4)
    print(f"{market} {name}: return {m['total_return_%']:.1f}%  CAGR {m['cagr_%']:.1f}%  "
          f"maxDD {m['max_drawdown_%']:.1f}%  Sharpe {m['sharpe']:.2f}")
