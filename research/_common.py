"""Shared helpers for research scripts. Data comes from data/cache (downloaded on first use)."""
from __future__ import annotations

import logging

import pandas as pd

from tradingbot.backtest import BacktestParams
from tradingbot.config import DEFAULT_SYMBOLS, Settings
from tradingbot.data import _cache_path, load_history

logging.disable(logging.INFO)
BASES = [s.split("/")[0] for s in DEFAULT_SYMBOLS]


def load(market: str, base: str, since: str = "2019-01-01") -> pd.DataFrame:
    """Use the local cache if it exists (run `python -m tradingbot portfolio` once to fill it)."""
    s = Settings(market=market, symbols=[f"{base}/USDT"]).validate()
    path = _cache_path(market, s.symbols[0], "4h")
    if path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df[df.index >= pd.Timestamp(since, tz="UTC")]
    return load_history(s, s.symbols[0], "4h", since)


def params(market: str, **kw) -> BacktestParams:
    p = BacktestParams(fee=0.001 if market == "spot" else 0.0005, slippage=0.0005,
                       leverage_cap=1 if market == "spot" else 3, allow_short=market == "futures",
                       funding_rate_8h=0.0001 if market == "futures" else 0.0, bar_hours=4)
    for k, v in kw.items():
        setattr(p, k, v)
    return p
