"""Historical data download with on-disk cache (data/cache/, git-ignored)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT, Settings
from .exchange import fetch_ohlcv_range, make_exchange

CACHE_DIR = PROJECT_ROOT / "data" / "cache"


def _cache_path(market: str, symbol: str, timeframe: str) -> Path:
    return CACHE_DIR / f"{market}_{symbol.replace('/', '').replace(':', '_')}_{timeframe}.csv"


def load_history(settings: Settings, symbol: str, timeframe: str, since: str,
                 until: str | None = None, refresh: bool = False) -> pd.DataFrame:
    """Load candles from cache, downloading only the missing tail from Binance (public data, no keys)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(settings.market, symbol, timeframe)
    since_ts = pd.Timestamp(since, tz="UTC")
    until_ts = pd.Timestamp(until, tz="UTC") if until else None

    cached = None
    if path.exists() and not refresh:
        cached = pd.read_csv(path, index_col=0, parse_dates=True)
        if cached.index.tz is None:
            cached.index = cached.index.tz_localize("UTC")

    ex = make_exchange(settings, authenticated=False)
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    until_ms = int(until_ts.timestamp() * 1000) if until_ts is not None else ex.milliseconds()

    if cached is not None and len(cached) and cached.index[0] <= since_ts:
        start_ms = int(cached.index[-1].timestamp() * 1000) + tf_ms
        fresh = fetch_ohlcv_range(ex, symbol, timeframe, start_ms, until_ms) if start_ms < until_ms else None
        df = pd.concat([cached, fresh]) if fresh is not None and len(fresh) else cached
    else:
        df = fetch_ohlcv_range(ex, symbol, timeframe, int(since_ts.timestamp() * 1000), until_ms)

    df = df[~df.index.duplicated(keep="last")].sort_index()
    # drop the candle that is still forming
    now_ms = ex.milliseconds()
    if len(df) and int(df.index[-1].timestamp() * 1000) + tf_ms > now_ms:
        df = df.iloc[:-1]
    df.to_csv(path)

    out = df[df.index >= since_ts]
    if until_ts is not None:
        out = out[out.index < until_ts]
    return out
