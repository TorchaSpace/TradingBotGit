"""Historical data download with on-disk cache (data/cache/, git-ignored)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT, Settings
from .exchange import fetch_ohlcv_range, make_exchange

CACHE_DIR = PROJECT_ROOT / "data" / "cache"


_CLIENTS: dict = {}


def _public_client(settings: Settings):
    """Reuse one public client per market so markets are loaded once (exchangeInfo is large)."""
    key = (settings.market, settings.mode == "demo")
    if key not in _CLIENTS:
        _CLIENTS[key] = make_exchange(settings, authenticated=False)
    return _CLIENTS[key]


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

    ex = _public_client(settings)
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    until_ms = int(until_ts.timestamp() * 1000) if until_ts is not None else ex.milliseconds()

    # marker remembers the earliest date already requested, so coins listed AFTER `since`
    # are not re-downloaded from scratch on every call
    marker = path.with_suffix(".since")
    asked = pd.Timestamp(marker.read_text().strip()) if marker.exists() else None
    covered = cached is not None and len(cached) and (
        cached.index[0] <= since_ts or (asked is not None and asked <= since_ts))
    if covered:
        start_ms = int(cached.index[-1].timestamp() * 1000) + tf_ms
        fresh = fetch_ohlcv_range(ex, symbol, timeframe, start_ms, until_ms) if start_ms < until_ms else None
        df = pd.concat([cached, fresh]) if fresh is not None and len(fresh) else cached
    else:
        df = fetch_ohlcv_range(ex, symbol, timeframe, int(since_ts.timestamp() * 1000), until_ms)
        marker.write_text(str(since_ts))

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


def load_funding(base: str, since: str = "2019-09-01", refresh: bool = False) -> pd.Series:
    """USDⓈ-M funding rate history for BASE/USDT, cached in data/cache/funding_<BASE>USDT.csv.
    Returns the funding paid per 8h window, indexed by the window's END time (UTC)."""
    import ccxt
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"funding_{base}USDT.csv"
    ex = _CLIENTS.setdefault(("funding",), ccxt.binanceusdm({"enableRateLimit": True}))
    cached = None
    if path.exists() and not refresh:
        cached = pd.read_csv(path, index_col=0)
        cached.index = pd.to_datetime(cached.index, utc=True, format="mixed")
    start = int(cached.index[-1].timestamp() * 1000) + 1 if cached is not None and len(cached) \
        else ex.parse8601(f"{since}T00:00:00Z")
    rows = []
    now = ex.milliseconds()
    while start < now:
        batch = ex.fetch_funding_rate_history(f"{base}/USDT:USDT", since=start, limit=1000)
        if not batch:
            break
        rows += [(r["timestamp"], r["fundingRate"]) for r in batch]
        if batch[-1]["timestamp"] + 1 <= start:
            break
        start = batch[-1]["timestamp"] + 1
    fresh = pd.DataFrame(rows, columns=["timestamp", "funding"])
    if len(fresh):
        fresh["timestamp"] = pd.to_datetime(fresh.timestamp, unit="ms", utc=True)
        fresh = fresh.set_index("timestamp")
    df = pd.concat([cached, fresh]) if cached is not None and len(fresh) else (cached if cached is not None else fresh)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(path)
    return funding_8h(df["funding"])


def funding_8h(f: pd.Series) -> pd.Series:
    """Sum payments into 8h windows labelled by their end (handles 4h/1h funding intervals too)."""
    f = f.copy()
    f.index = pd.DatetimeIndex(f.index).floor("min")
    return f.resample("8h", label="right", closed="right").sum(min_count=1)


ARCHIVE_URL = "https://data.binance.vision/data/spot/monthly/klines/{s}/{tf}/{s}-{tf}-{y}-{m:02d}.zip"


def load_hourly_archive(symbol: str, since: str, timeframe: str = "1h", workers: int = 8) -> int:
    """Bulk-fill the spot cache from Binance's public monthly archive (data.binance.vision): years of
    candles in seconds instead of thousands of API calls. Afterwards load_history only fetches the tail.
    Returns the number of candles written (0 when the cache already covers `since`)."""
    import io
    import urllib.error
    import urllib.request
    import zipfile
    from concurrent.futures import ThreadPoolExecutor

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path("spot", symbol, timeframe)
    marker = path.with_suffix(".since")
    since_ts = pd.Timestamp(since, tz="UTC")
    if path.exists() and marker.exists() and pd.Timestamp(marker.read_text().strip()) <= since_ts:
        return 0
    pair = symbol.replace("/", "")
    now = pd.Timestamp.now(tz="UTC")
    months = pd.date_range(since_ts.normalize().replace(day=1), now - pd.offsets.MonthBegin(1), freq="MS")

    def get(m):
        url = ARCHIVE_URL.format(s=pair, tf=timeframe, y=m.year, m=m.month)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                raw = zipfile.ZipFile(io.BytesIO(r.read())).read(f"{pair}-{timeframe}-{m.year}-{m.month:02d}.csv")
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, zipfile.BadZipFile, TimeoutError):
            return None
        rows = [ln.split(",")[:6] for ln in raw.decode().splitlines() if ln[:1].isdigit()]
        return rows or None

    with ThreadPoolExecutor(workers) as pool:
        parts = [r for r in pool.map(get, months) if r]
    if not parts:
        return 0
    a = pd.DataFrame([r for part in parts for r in part], dtype=float).to_numpy()
    ts = a[:, 0]
    ts = pd.Series(ts).where(pd.Series(ts) < 1e14, pd.Series(ts) / 1000).to_numpy()   # 2025+ archives: microseconds
    df = pd.DataFrame(a[:, 1:6], columns=["open", "high", "low", "close", "volume"],
                      index=pd.to_datetime(ts, unit="ms", utc=True).floor(timeframe.replace("m", "min")))
    df.index.name = "timestamp"
    if path.exists():
        old = pd.read_csv(path, index_col=0, parse_dates=True)
        if old.index.tz is None:
            old.index = old.index.tz_localize("UTC")
        df = pd.concat([df, old])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(path)
    marker.write_text(str(since_ts))
    return int(len(df))
