"""Second price source: before a REAL-money entry, compare Binance's price with Coinbase / Kraken.

Protects against a bad tick or a broken Binance feed. Public data only (no keys). If no second
source quotes the coin, or they cannot be reached, the check passes (it never blocks on its own
outage) and says so in the log.
"""
from __future__ import annotations

import logging
import threading
import time

import ccxt

from .execution import MarketQualityError

log = logging.getLogger(__name__)
MAX_DEVIATION = 0.02      # 2%
CACHE_SECONDS = 30
_VENUES = (("coinbase", ("USD", "USDT")), ("kraken", ("USD", "USDT")))


class PriceCheck:
    def __init__(self, factories: dict | None = None):
        self._factories = factories or {name: getattr(ccxt, name) for name, _ in _VENUES}
        self._clients: dict = {}
        self._cache: dict[str, tuple[float, float | None, str]] = {}
        self._lock = threading.Lock()

    def _client(self, name: str):
        if name not in self._clients:
            self._clients[name] = self._factories[name]({"enableRateLimit": True, "timeout": 8000})
        return self._clients[name]

    def reference(self, base: str) -> tuple[float | None, str]:
        """(price, source) from the first venue that quotes BASE/USD or BASE/USDT, else (None, '')."""
        with self._lock:
            hit = self._cache.get(base)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1], hit[2]
        price, src = None, ""
        for name, quotes in _VENUES:
            try:
                ex = self._client(name)
                if not ex.markets:
                    ex.load_markets()
                for q in quotes:
                    sym = f"{base}/{q}"
                    if sym in ex.markets:
                        price, src = float(ex.fetch_ticker(sym)["last"]), f"{name} {sym}"
                        break
            except Exception as e:  # second source down: never block trading because of it
                log.debug("price check %s via %s failed: %s", base, name, e)
            if price:
                break
        with self._lock:
            self._cache[base] = (time.time(), price, src)
        return price, src

    def verify(self, symbol: str, binance_price: float) -> float | None:
        """Raise MarketQualityError when the two prices differ by more than MAX_DEVIATION."""
        base = symbol.split("/")[0]
        ref, src = self.reference(base)
        if not ref:
            log.info("%s: ikinci fiyat kaynağı yok, kontrol atlandı", symbol)
            return None
        dev = abs(binance_price / ref - 1)
        if dev > MAX_DEVIATION:
            raise MarketQualityError(f"{symbol}: Binance fiyatı {binance_price:.6g}, {src} {ref:.6g} "
                                     f"(fark %{dev * 100:.1f}); hatalı fiyat olabilir, giriş yapılmadı")
        return dev


_DEFAULT: PriceCheck | None = None


def default() -> PriceCheck:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PriceCheck()
    return _DEFAULT
