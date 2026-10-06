"""Shared helpers for research scripts. Data comes from data/cache (downloaded on first use)."""
from __future__ import annotations

import logging

import pandas as pd

from tradingbot.backtest import BacktestParams
from tradingbot.config import DEFAULT_SYMBOLS, Settings
from tradingbot.data import _cache_path, load_history

logging.disable(logging.INFO)
BASES = [s.split("/")[0] for s in DEFAULT_SYMBOLS]
WIDE = BASES + ["DOT", "LTC", "TRX", "AVAX", "ATOM", "UNI", "FIL", "ETC", "XLM", "NEAR", "AAVE",
                "BCH", "ALGO", "VET", "EOS", "HBAR"]   # 24 coins incl. EOS (delisted 2025)
SPLIT, END = "2025-01-01", "2026-10-06"  # tune on data before SPLIT, judge on SPLIT..END only


def universe(market: str, bases: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for b in bases:
        try:
            out[b] = load(market, b)
        except Exception:  # e.g. no futures market for this coin
            pass
    return out


def portfolio(market, full, targets, start, end, max_positions=6, risk_scale=None, **kw):
    from tradingbot.backtest import run_portfolio
    dfs = {b: d[(d.index >= pd.Timestamp(start, tz="UTC")) & (d.index < pd.Timestamp(end, tz="UTC"))]
           for b, d in full.items()}
    dfs = {b: d for b, d in dfs.items() if len(d) > 50}
    kw.setdefault("atr_mult", 5)
    kw.setdefault("trailing", False)
    kw.setdefault("risk_per_trade", 0.005)
    return run_portfolio(dfs, {b: targets[b].loc[dfs[b].index] for b in dfs}, params(market, **kw),
                         max_positions=max_positions, risk_scale=risk_scale)


def fmt(r) -> str:
    m = r.metrics
    return (f"CAGR {m['cagr_%']:6.1f}%  maxDD {m['max_drawdown_%']:6.1f}%  "
            f"Sharpe {m['sharpe']:5.2f}  win {m['win_rate_%']:4.1f}%")


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
