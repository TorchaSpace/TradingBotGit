"""Strategies: pure functions, candles -> target position per bar.

Return value: pd.Series of {-1, 0, 1} aligned to df.index. The value at bar t is decided using
data up to the CLOSE of bar t, and the engine/live bot acts on it at the OPEN of bar t+1.
Stops are handled by the engine (ATR-based), not by the strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .indicators import adx, donchian, ema, higher_tf_trend, rsi


def ema_trend(df: pd.DataFrame, allow_short: bool, fast: int = 20, slow: int = 50,
              trend: int = 200) -> pd.Series:
    """Trend following: fast/slow EMA alignment, filtered by long-term EMA."""
    c = df["close"]
    f, s, t = ema(c, fast), ema(c, slow), ema(c, trend)
    long_ = (f > s) & (c > t)
    short = (f < s) & (c < t)
    out = pd.Series(0, index=df.index, dtype=int)
    out[long_] = 1
    if allow_short:
        out[short] = -1
    out[t.isna()] = 0
    return out


def rsi_reversion(df: pd.DataFrame, allow_short: bool, n: int = 14, low: float = 30,
                  high: float = 70, exit_long: float = 55, exit_short: float = 45,
                  trend: int = 200) -> pd.Series:
    """Mean reversion: buy oversold dips in an uptrend, exit on RSI recovery (mirror for shorts)."""
    c = df["close"].to_numpy()
    r = rsi(df["close"], n).to_numpy()
    t = ema(df["close"], trend).to_numpy()
    out = np.zeros(len(df), dtype=int)
    pos = 0
    for i in range(len(df)):
        if np.isnan(r[i]) or np.isnan(t[i]):
            out[i] = 0
            continue
        if pos == 1 and r[i] >= exit_long:
            pos = 0
        elif pos == -1 and r[i] <= exit_short:
            pos = 0
        if pos == 0:
            if r[i] < low and c[i] > t[i]:
                pos = 1
            elif allow_short and r[i] > high and c[i] < t[i]:
                pos = -1
        out[i] = pos
    return pd.Series(out, index=df.index)


def donchian_breakout(df: pd.DataFrame, allow_short: bool, entry: int = 20,
                      exit_: int = 10) -> pd.Series:
    """Turtle-style breakout: enter on close beyond the N-bar channel, exit on the shorter channel."""
    up_e, lo_e = donchian(df, entry)
    up_x, lo_x = donchian(df, exit_)
    c = df["close"].to_numpy()
    ue, le, ux, lx = (x.to_numpy() for x in (up_e, lo_e, up_x, lo_x))
    out = np.zeros(len(df), dtype=int)
    pos = 0
    for i in range(len(df)):
        if np.isnan(ue[i]) or np.isnan(ux[i]):
            continue
        if pos == 1 and c[i] < lx[i]:
            pos = 0
        elif pos == -1 and c[i] > ux[i]:
            pos = 0
        if pos == 0:
            if c[i] > ue[i]:
                pos = 1
            elif allow_short and c[i] < le[i]:
                pos = -1
        out[i] = pos
    return pd.Series(out, index=df.index)


def gate_entries(base: pd.Series, allowed: pd.Series) -> pd.Series:
    """New positions only where `allowed` (+1 longs, -1 shorts, 0 none) agrees; an open
    position is then held as long as the base signal keeps it (the filter does not force exits)."""
    b = base.to_numpy()
    al = allowed.reindex(base.index).fillna(0).to_numpy()
    out = np.zeros(len(b), dtype=int)
    pos = 0
    for i in range(len(b)):
        if pos != 0 and b[i] != pos:
            pos = 0
        if pos == 0 and b[i] != 0 and (al[i] == b[i] or al[i] == 2):
            pos = int(b[i])
        out[i] = pos
    return pd.Series(out, index=base.index)


def filters(df: pd.DataFrame, adx_min: float = 0.0, htf: bool = False, htf_n: int = 50) -> pd.Series:
    """Entry permission: 2 = both directions allowed, +1 long only, -1 short only, 0 none."""
    allow = pd.Series(2.0, index=df.index)
    if adx_min > 0:
        allow[(adx(df) < adx_min).to_numpy()] = 0
    if htf:
        t = higher_tf_trend(df, "1D", htf_n)
        allow = np.where(allow == 0, 0, t)
        allow = pd.Series(allow, index=df.index)
    return allow


@dataclass(frozen=True)
class StrategySpec:
    name: str
    fn: Callable[..., pd.Series]
    description: str
    warmup: int  # bars needed before signals are valid


STRATEGIES: dict[str, StrategySpec] = {
    "ema_trend": StrategySpec("ema_trend", ema_trend, "EMA 20/50 trend + EMA200 filter", 210),
    "rsi_reversion": StrategySpec("rsi_reversion", rsi_reversion, "RSI(14) dip-buy in trend", 210),
    "donchian_breakout": StrategySpec("donchian_breakout", donchian_breakout, "Donchian 20/10 breakout", 30),
}


def get_strategy(name: str) -> StrategySpec:
    if name not in STRATEGIES:
        raise KeyError(f"Unknown strategy {name!r}. Options: {', '.join(STRATEGIES)}")
    return STRATEGIES[name]
