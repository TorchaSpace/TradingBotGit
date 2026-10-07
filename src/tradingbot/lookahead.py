"""Look-ahead bias check (`python -m tradingbot lookahead`), like Freqtrade's lookahead-analysis.

Every signal and feature is computed twice: once on the full history and once on history cut at
several points. A value at time t must not change when the data after t is removed. If it does, the
calculation peeks into the future and every backtest built on it is too good to be true.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .strategies import STRATEGIES, apply_btc_filter


def _diff(full: pd.DataFrame | pd.Series, part: pd.DataFrame | pd.Series) -> int:
    """Number of values that differ between `part` and the same rows of `full`."""
    a = full.loc[part.index]
    if isinstance(part, pd.Series):
        a, part = a.to_frame(), part.to_frame()
    a = a[part.columns].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    b = part.apply(pd.to_numeric, errors="coerce").to_numpy(float)
    both_nan = np.isnan(a) & np.isnan(b)
    close = np.isclose(a, b, rtol=1e-7, atol=1e-12)
    return int((~(close | both_nan)).sum())


def check(fn, df: pd.DataFrame, cuts: int = 6, min_bars: int = 400) -> dict:
    """fn(df) -> Series/DataFrame indexed like df. Returns {'ok', 'mismatches', 'cuts'}."""
    full = fn(df)
    points = np.linspace(min_bars, len(df) - 1, cuts).astype(int) if len(df) > min_bars + cuts else []
    bad = 0
    for k in points:
        part = fn(df.iloc[:k])
        bad += _diff(full, part)
    return {"ok": bad == 0, "mismatches": bad, "cuts": len(points)}


def check_all(full: dict[str, pd.DataFrame], btc: pd.DataFrame | None, allow_short: bool = False) -> list[dict]:
    """All strategies (with and without the BTC filter), the learner and ML-agent features."""
    from . import learner, mlagent
    out = []
    sym, df = next(iter(full.items()))
    for name, spec in STRATEGIES.items():
        out.append({"what": f"strateji {name}", **check(lambda d, f=spec.fn: f(d, allow_short), df)})
        if btc is not None:
            b = btc

            def with_filter(d, f=spec.fn):
                return apply_btc_filter(f(d, allow_short), b[b.index <= d.index[-1]])
            out.append({"what": f"strateji {name} + BTC filtresi", **check(with_filter, df)})
    out.append({"what": "öğrenen filtre özellikleri", **check(lambda d: learner.feature_frame(d, btc[btc.index <= d.index[-1]]
                                                                                           if btc is not None else None), df)})
    small = dict(list(full.items())[:3])
    if btc is not None:
        small.setdefault("BTC/USDT", btc)

    def ml(d):
        end = d.index[-1]
        cut = {k: v[v.index <= end] for k, v in small.items()}
        X = mlagent.features(cut, cut.get("BTC/USDT", next(iter(cut.values()))))
        return X[X["coin"] == sym][mlagent.FEATURES]
    out.append({"what": "ML ajan özellikleri", **check(ml, df, cuts=4, min_bars=1800)})
    return out
