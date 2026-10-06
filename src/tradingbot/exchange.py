"""Binance connection via ccxt. Only this module and live.py talk to the network.

- spot    -> ccxt.binance
- futures -> ccxt.binanceusdm (USDⓈ-M perpetuals)
- MODE=demo uses Binance Demo Trading (demo-api / demo-fapi) via ccxt.enable_demo_trading.
- Conditional (stop) orders on USDⓈ-M are routed by ccxt to /fapi/v1/algoOrder automatically.
"""
from __future__ import annotations

import logging
import time

import ccxt
import pandas as pd

from .config import Settings

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def make_exchange(settings: Settings, authenticated: bool | None = None) -> ccxt.Exchange:
    """Build a ccxt client. `authenticated=False` gives a public (market-data only) client."""
    cls = ccxt.binanceusdm if settings.market == "futures" else ccxt.binance
    use_keys = authenticated if authenticated is not None else settings.mode in ("demo", "live")
    cfg: dict = {
        "enableRateLimit": True,  # ccxt throttles requests to stay under Binance weight limits
        "options": {"adjustForTimeDifference": True, "recvWindow": 10000},
    }
    if use_keys:
        cfg["apiKey"] = settings.api_key
        cfg["secret"] = settings.api_secret
    ex = cls(cfg)
    if settings.mode == "demo":
        ex.enable_demo_trading(True)
    return ex


def ohlcv_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df.astype(float)


def fetch_ohlcv_range(ex: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int,
                      until_ms: int | None = None, limit: int = 1000) -> pd.DataFrame:
    """Paginated historical candle download."""
    until_ms = until_ms or ex.milliseconds()
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    rows: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = _retry(lambda: ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit))
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= cursor:
            break
        cursor = nxt
    rows = [r for r in rows if r[0] < until_ms]
    return ohlcv_to_df(rows)


def fetch_recent_closed(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    """Recent candles with the still-forming last candle removed (no look-ahead)."""
    rows = _retry(lambda: ex.fetch_ohlcv(symbol, timeframe, limit=limit))
    df = ohlcv_to_df(rows)
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    now_ms = ex.milliseconds()
    if len(df) and int(df.index[-1].timestamp() * 1000) + tf_ms > now_ms:
        df = df.iloc[:-1]
    return df


def _retry(fn, attempts: int = 5, base_delay: float = 1.0):
    for i in range(attempts):
        try:
            return fn()
        except ccxt.DDoSProtection as e:  # 429 / 418
            if "418" in str(e):
                log.critical("Binance IP ban (418) received. Stopping requests. %s", e)
                raise
            delay = base_delay * 2 ** i
            log.warning("Rate limited, sleeping %.1fs: %s", delay, e)
            time.sleep(delay)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            delay = base_delay * 2 ** i
            log.warning("Network error, retry in %.1fs: %s", delay, e)
            time.sleep(delay)
    return fn()
