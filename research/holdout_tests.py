"""Which improvements survive data they were NOT tuned on?

Every idea is measured on 2021-2024 (TRAIN) and on 2025-01..2026-10 (HOLDOUT, never used to choose
anything). Only ideas that help in BOTH are kept in the bot.

    python research/holdout_tests.py spot futures

Result (2026-10):
  BTC regime filter ............ better in train AND holdout (spot 8c: Sharpe 0.65 -> 0.74,
                                 DD -15.5% -> -13.7%; futures 8c: 0.38 -> 0.60)  -> ENABLED
  24 coins instead of 8 ........ worse (the 8 big coins are partly survivors -> optimistic)
  halve risk in drawdown ....... worse in holdout
  breakeven stop / ATR 4 or 6 .. no robust gain
  spot + futures together ...... returns 83% correlated, no diversification gain
"""
import sys

from _common import BASES, END, SPLIT, WIDE, fmt, portfolio, universe
from tradingbot.strategies import make_target

for m in sys.argv[1:] or ["spot"]:
    f8, f24 = universe(m, BASES), universe(m, WIDE)
    short = m == "futures"
    tg = lambda full, flt: {b: make_target("ema_trend", d, short, full["BTC"] if flt else None)
                            for b, d in full.items()}
    t8, t8b, t24, t24b = tg(f8, False), tg(f8, True), tg(f24, False), tg(f24, True)
    cases = [
        ("8 coins (old default)", f8, t8, {}),
        ("8 coins + BTC filter (new)", f8, t8b, {}),
        ("24 coins", f24, t24, {}),
        ("24 coins + BTC filter", f24, t24b, {}),
        ("24c + BTC, 10 pos, 0.4% risk", f24, t24b, dict(max_positions=10, risk_per_trade=0.004)),
        ("8c + BTC + halve risk in DD>10%", f8, t8b, dict(dd_risk_cut=0.10)),
        ("8c + BTC + breakeven at 3 ATR", f8, t8b, dict(breakeven_atr=3)),
        ("8c + BTC + 4x ATR stop", f8, t8b, dict(atr_mult=4)),
        ("8c + BTC + 6x ATR stop", f8, t8b, dict(atr_mult=6)),
    ]
    for name, full, t, kw in cases:
        a = portfolio(m, full, t, "2021-01-01", SPLIT, **kw)
        b = portfolio(m, full, t, SPLIT, END, **kw)
        print(f"{m:7s} {name:34s} TRAIN {fmt(a)} || HOLDOUT {fmt(b)}", flush=True)
