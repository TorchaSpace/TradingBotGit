"""Pure indicator functions (pandas only, no I/O). All use data up to and including the current bar."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def donchian(df: pd.DataFrame, n: int) -> tuple[pd.Series, pd.Series]:
    """Channel of the PREVIOUS n bars (excludes current bar) so a breakout can be detected on close."""
    upper = df["high"].rolling(n).max().shift(1)
    lower = df["low"].rolling(n).min().shift(1)
    return upper, lower


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average Directional Index (trend strength, 0-100). Wilder smoothing."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def higher_tf_trend(df: pd.DataFrame, rule: str = "1D", n: int = 50) -> pd.Series:
    """+1 / -1 / 0: is the last COMPLETED higher-timeframe close above/below its EMA(n)?

    The daily bar of day D is only known after D ends, so it is shifted by one period
    before being mapped back onto the lower timeframe (no look-ahead).
    """
    htf = df["close"].resample(rule, label="left", closed="left").last().dropna()
    e = ema(htf, n)
    sign = np.sign(htf - e).where(e.notna(), 0.0)
    sign = sign.shift(1)  # only completed bars
    return sign.reindex(df.index, method="ffill").fillna(0.0)
