"""Coin safety filter (like Freqtrade's DelistFilter / VolumePairList / AgeFilter), run once an hour.

A coin gets NO NEW ENTRIES when:
  - Binance has stopped trading it or announced its delisting (delist schedule, live keys only),
  - its 24h volume is below MIN_QUOTE_VOLUME (thin market: big slippage, easy to manipulate),
  - it has been listed for less than MIN_AGE_DAYS (no history to judge it).
A position the bot already holds in a coin that will be delisted within DELIST_EXIT_DAYS is closed
early, so the bot is never stuck holding a coin Binance removes.
Volume and listing age come from Binance's real public data (also in demo, whose volumes are fake).
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from .config import Settings

log = logging.getLogger(__name__)
MIN_AGE_DAYS = 90
DELIST_EXIT_DAYS = 7
REFRESH_SECONDS = 3600


class PairFilter:
    def __init__(self, s: Settings, trading_ex, public_ex=None, auth_ex=None):
        """trading_ex: the exchange the bot trades on (market status); public_ex: real Binance public data;
        auth_ex: authenticated real (live) client for the delist schedule, or None."""
        from .exchange import make_exchange
        self.s = s
        self.trading_ex = trading_ex
        self.public_ex = public_ex or make_exchange(
            Settings(mode="paper", market=s.market, symbols=list(s.symbols)).validate(), authenticated=False)
        self.auth_ex = auth_ex
        self.blocked: dict[str, str] = {}
        self.delisting: dict[str, pd.Timestamp] = {}
        self._listed: dict[str, pd.Timestamp] = {}
        self._last = 0.0

    def refresh(self, force: bool = False) -> dict[str, str]:
        if not force and time.time() - self._last < REFRESH_SECONDS:
            return self.blocked
        self._last = time.time()
        blocked: dict[str, str] = {}
        delisting = self._delist_schedule()
        try:
            markets = self.trading_ex.load_markets()
        except Exception as e:
            log.debug("pair filter: markets unavailable (%s)", e)
            markets = {}
        try:
            tickers = self.public_ex.fetch_tickers(self.s.symbols)
        except Exception as e:
            log.debug("pair filter: tickers unavailable (%s)", e)
            tickers = {}
        for sym in self.s.symbols:
            m = markets.get(sym) if markets else None
            if markets and (m is None or m.get("active") is False):
                blocked[sym] = "Binance'te işleme kapalı"
                continue
            if sym in delisting:
                blocked[sym] = f"Binance listeden çıkarıyor ({delisting[sym].date()})"
                continue
            qv = (tickers.get(sym) or {}).get("quoteVolume")
            if qv is not None and float(qv) < self.s.min_quote_volume:
                blocked[sym] = f"24s hacim düşük ({float(qv) / 1e6:.1f}M USDT < {self.s.min_quote_volume / 1e6:.0f}M)"
                continue
            listed = self._listing_date(sym)
            if listed is not None and pd.Timestamp.now(tz="UTC") - listed < pd.Timedelta(days=MIN_AGE_DAYS):
                blocked[sym] = f"yeni listelenmiş ({listed.date()})"
        for sym, why in blocked.items():
            if self.blocked.get(sym) != why:
                log.warning("%s: yeni işlem açılmayacak: %s", sym, why)
        for sym in set(self.blocked) - set(blocked):
            log.info("%s: coin filtresinden çıktı, işlem açılabilir", sym)
        self.blocked, self.delisting = blocked, delisting
        return blocked

    def must_exit(self, sym: str) -> str | None:
        when = self.delisting.get(sym)
        if when is not None and when - pd.Timestamp.now(tz="UTC") <= pd.Timedelta(days=DELIST_EXIT_DAYS):
            return f"Binance {when.date()} tarihinde listeden çıkarıyor"
        return None

    def _delist_schedule(self) -> dict[str, pd.Timestamp]:
        if self.auth_ex is None or self.s.market != "spot":
            return {}
        try:
            rows = self.auth_ex.sapi_get_spot_delist_schedule()
        except Exception as e:
            log.debug("delist schedule unavailable: %s", e)
            return {}
        out = {}
        for r in rows or []:
            t = pd.Timestamp(int(r.get("delistTime")), unit="ms", tz="UTC")
            for raw in r.get("symbols") or []:
                for sym in self.s.symbols:
                    if sym.replace("/", "").split(":")[0] == raw:
                        out[sym] = t
        return out

    def _listing_date(self, sym: str) -> pd.Timestamp | None:
        if sym not in self._listed:
            try:
                rows = self.public_ex.fetch_ohlcv(sym, "1d", since=1_500_000_000_000, limit=1)
                self._listed[sym] = pd.Timestamp(rows[0][0], unit="ms", tz="UTC") if rows else None
            except Exception:
                return None
        return self._listed[sym]
