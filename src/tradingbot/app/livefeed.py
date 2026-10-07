"""Live market view for the app: prices, candles and a plain-language reading of each chart.

Read-only. It never places orders. Decisions stay with the bot (on closed candles, as tested);
this module shows, second by second, what the bot sees and how close each coin is to a signal.
Uses a public client of the same venue the bot trades on (Binance Demo prices in demo mode).
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd

from .. import exchange as _exchange
from ..config import Settings
from ..exchange import ohlcv_to_df
from ..indicators import atr, ema
from ..strategies import make_target

PRICE_TTL = 3.0      # seconds between ticker refreshes
CANDLE_TTL = 20.0    # seconds between candle refreshes (the forming candle is patched with live prices)
VIEW_TIMEFRAMES = ("15m", "1h", "4h", "1d")


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


class LiveFeed:
    def __init__(self, factory=None):
        self._factory = factory or (lambda s: _exchange.make_exchange(s, authenticated=False))
        self._lock = threading.Lock()
        self._venue = None
        self._ex = None
        self._tickers: tuple[float, dict] = (0.0, {})
        self._candles: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    # ------------------------------------------------------------------ data
    def _client(self, s: Settings):
        venue = (s.mode, s.market)
        if venue != self._venue:
            self._ex, self._venue = self._factory(s), venue
            self._tickers, self._candles = (0.0, {}), {}
        return self._ex

    def prices(self, s: Settings) -> dict[str, dict]:
        with self._lock:
            ex = self._client(s)
            t, cached = self._tickers
            if time.time() - t < PRICE_TTL and all(x in cached for x in s.symbols):
                return cached
            out = {}
            try:
                raw = ex.fetch_tickers(s.symbols)
            except Exception:
                raw = {}
                for sym in s.symbols:
                    try:
                        raw[sym] = ex.fetch_ticker(sym)
                    except Exception:
                        pass
            for sym, tk in raw.items():
                last = tk.get("last")
                if last is None:
                    continue
                out[sym] = {"last": float(last), "change_24h": tk.get("percentage"),
                            "high_24h": tk.get("high"), "low_24h": tk.get("low")}
            self._tickers = (time.time(), out)
            return out

    def candles(self, s: Settings, symbol: str, timeframe: str, limit: int = 400) -> pd.DataFrame:
        """Candles incl. the still-forming one (its close/high/low patched with the live price)."""
        with self._lock:
            ex = self._client(s)
            key = (symbol, timeframe)
            t, df = self._candles.get(key, (0.0, None))
            if df is None or time.time() - t > CANDLE_TTL:
                df = ohlcv_to_df(ex.fetch_ohlcv(symbol, timeframe, limit=limit))
                self._candles[key] = (time.time(), df)
            tick = self._tickers[1].get(symbol)
        df = df.copy()
        if tick and len(df):
            px = tick["last"]
            i = df.index[-1]
            df.loc[i, "close"] = px
            df.loc[i, "high"] = max(df.loc[i, "high"], px)
            df.loc[i, "low"] = min(df.loc[i, "low"], px)
        return df

    # ------------------------------------------------------------------ reading
    def read(self, s: Settings, symbol: str, position: dict | None, btc: pd.DataFrame | None) -> dict:
        """What the bot sees on its own timeframe right now, in numbers and in words."""
        df = self.candles(s, symbol, s.timeframe)
        tf_ms = int(pd.Timedelta(s.timeframe.replace("m", "min")).total_seconds() * 1000)
        now_ms = int(time.time() * 1000)
        forming = len(df) and int(df.index[-1].timestamp() * 1000) + tf_ms > now_ms
        closed = df.iloc[:-1] if forming else df
        c = closed["close"]
        live_px = float(df["close"].iloc[-1])
        e20, e50, e200 = (float(ema(c, n).iloc[-1]) for n in (20, 50, 200))
        a = float(atr(closed).iloc[-1])
        flt = None
        if s.btc_filter and btc is not None:
            flt = btc[btc.index <= closed.index[-1]] if len(closed) else btc
        tgt_closed = int(make_target(s.strategy, closed, s.allow_short, flt).iloc[-1])
        tgt_now = int(make_target(s.strategy, df, s.allow_short, btc if s.btc_filter else None).iloc[-1])
        btc_up = None
        if btc is not None and len(btc) > 200:
            bc = btc["close"]
            btc_up = bool(bc.iloc[-1] > ema(bc, 200).iloc[-1])
        next_ms = (int(closed.index[-1].timestamp() * 1000) + 2 * tf_ms) if len(closed) else None
        r = {"symbol": symbol, "price": live_px, "ema20": e20, "ema50": e50, "ema200": e200, "atr": a,
             "signal_closed": tgt_closed, "signal_if_closed_now": tgt_now, "btc_up": btc_up,
             "next_decision": pd.Timestamp(next_ms, unit="ms", tz="UTC").isoformat() if next_ms else None,
             "trend_gap_pct": _pct(e20, e50), "vs_ema200_pct": _pct(live_px, e200)}
        lines = []
        if np.isnan(e200):
            lines.append("Yeterli geçmiş veri yok.")
        else:
            lines.append(f"Fiyat uzun vadeli ortalamanın (EMA200) %{abs(r['vs_ema200_pct']):.1f} "
                         f"{'üstünde: uzun vadeli yükseliş' if live_px > e200 else 'altında: uzun vadeli düşüş'}.")
            lines.append(f"Kısa vade: EMA20, EMA50'nin %{abs(r['trend_gap_pct']):.2f} "
                         f"{'üstünde (yukarı ivme)' if e20 > e50 else 'altında (aşağı ivme)'}.")
            if s.btc_filter and btc_up is not None:
                lines.append("BTC yükseliş trendinde: long'lara izin var." if btc_up else
                             "BTC düşüş trendinde: yeni long açılmaz" + (", short'lara izin var." if s.allow_short else "."))
        pos = None
        if position:
            d = int(position.get("direction", 1))
            entry, stop, qty = float(position["entry"]), float(position["stop"]), float(position["qty"])
            pnl = (live_px - entry) * qty * d
            pos = {"direction": d, "entry": entry, "stop": stop, "qty": qty, "pnl": pnl,
                   "pnl_pct": _pct(live_px, entry) * d, "to_stop_pct": abs(_pct(stop, live_px)),
                   "risk_usdt": abs(entry - stop) * qty}
            lines.append(f"{'LONG' if d == 1 else 'SHORT'} pozisyon açık: giriş {entry:.6g}, şu an {live_px:.6g} "
                         f"({pos['pnl_pct']:+.2f}%, {pnl:+.2f} USDT). Stop %{pos['to_stop_pct']:.1f} uzakta, borsada bekliyor.")
            if tgt_now != d:
                lines.append("Dikkat: mum şu an kapansa çıkış sinyali oluşur. Karar mum kapanışında verilecek.")
            else:
                lines.append("Trend devam ediyor; çıkış için EMA20'nin EMA50'nin altına inmesi ya da stop gerekiyor."
                             if d == 1 else "Trend devam ediyor; çıkış için EMA20'nin EMA50'nin üstüne çıkması ya da stop gerekiyor.")
        else:
            if tgt_closed != 0:
                lines.append("Son kapanan mumda sinyal var; bot pozisyonu açmış olmalı ya da risk kuralları izin vermedi.")
            elif tgt_now != 0:
                lines.append(f"Mum şu an kapansa {'LONG' if tgt_now == 1 else 'SHORT'} sinyali oluşur. "
                             "Bot kapanışı bekliyor (yarım mumla işlem testlerde zarar ettirdi).")
            else:
                need = []
                if e20 <= e50:
                    need.append(f"EMA20'nin EMA50'yi yukarı kesmesi (%{abs(r['trend_gap_pct']):.2f} uzak)")
                if live_px <= e200:
                    need.append(f"fiyatın EMA200'ün üstüne çıkması (%{abs(r['vs_ema200_pct']):.1f} uzak)")
                if s.btc_filter and btc_up is False:
                    need.append("BTC'nin yükseliş trendine dönmesi")
                lines.append("Sinyal yok. Long için gereken: " + ("; ".join(need) if need else
                             "koşullar sağlanıyor, bir önceki pozisyon stop olduysa sinyal sıfırlanmayı bekliyor") + ".")
        r["position"] = pos
        r["lines"] = lines
        r["status"] = "open" if pos else ("signal" if tgt_closed != 0 else ("ready" if tgt_now != 0 else "wait"))
        return r

    def overview(self, s: Settings, open_trades: dict) -> dict:
        prices = self.prices(s)
        btc = None
        if s.btc_filter:
            try:
                btc = self.candles(s, s.btc_symbol, s.timeframe)
            except Exception:
                btc = None
        coins, errors = [], {}
        for sym in s.symbols:
            try:
                rd = self.read(s, sym, open_trades.get(sym), btc)
                rd.update(prices.get(sym, {}))
                coins.append(rd)
            except Exception as e:
                errors[sym] = f"{type(e).__name__}: {str(e)[:160]}"
        return {"coins": coins, "errors": errors, "timeframe": s.timeframe, "time": time.time()}

    def chart(self, s: Settings, symbol: str, timeframe: str, bars: int = 160) -> dict:
        if timeframe not in VIEW_TIMEFRAMES:
            timeframe = s.timeframe
        self.prices(s)
        df = self.candles(s, symbol, timeframe, limit=max(bars + 220, 400))
        c = df["close"]
        e = {n: ema(c, n) for n in (20, 50, 200)}
        tail = df.tail(bars)
        rows = [[int(t.timestamp() * 1000), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
                for t, r in tail.iterrows()]
        lines = {f"ema{n}": [None if np.isnan(v) else float(v) for v in e[n].tail(bars)] for n in e}
        return {"symbol": symbol, "timeframe": timeframe, "candles": rows, **lines}
