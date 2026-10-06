"""Live / demo / paper trading loop.

Flow per symbol, once per newly CLOSED candle:
  1. compute the strategy target on closed candles only
  2. exit if the target no longer matches the open position
  3. optionally trail the stop (TRAILING_STOP=true; off by default, see research/)
  4. enter if allowed by the RiskManager, sized by ATR stop distance

Safety:
  - MODE=paper by default; MODE=live requires LIVE_TRADING_CONFIRM (see config.py)
  - every position gets an exchange-side stop order (demo/live)
  - daily loss limit blocks new entries; max drawdown flattens everything and halts
  - create a file named STOP in the project root to stop the bot gracefully
  - on startup, positions on the exchange are reconciled with saved state
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

from .config import PROJECT_ROOT, Settings
from .exchange import fetch_recent_closed, make_exchange
from .execution import execute
from .notify import Notifier
from .indicators import atr as atr_fn
from .risk import RiskManager, initial_stop, position_size, trail_stop
from .strategies import get_strategy, make_target

log = logging.getLogger(__name__)

STATE_DIR = PROJECT_ROOT / "state"
STOP_FILE = PROJECT_ROOT / "STOP"


@dataclass
class Position:
    direction: int  # 1 long, -1 short
    qty: float
    entry: float


# --------------------------------------------------------------------------- brokers
class PaperBroker:
    """Simulated fills on real public prices. No keys, no real orders."""

    def __init__(self, settings: Settings, ex: ccxt.Exchange):
        self.s, self.ex = settings, ex
        self.state_file = STATE_DIR / f"paper_{settings.market}.json"
        self.cash = settings.paper_start_balance
        self.positions: dict[str, dict] = {}
        if self.state_file.exists():
            d = json.loads(self.state_file.read_text())
            self.cash, self.positions = d["cash"], d["positions"]

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"cash": self.cash, "positions": self.positions}, indent=2))

    def price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def equity(self) -> float:
        eq = self.cash
        for sym, p in self.positions.items():
            eq += p["direction"] * p["qty"] * (self.price(sym) - p["entry"])
        return eq

    def position(self, symbol: str) -> Position | None:
        p = self.positions.get(symbol)
        return Position(p["direction"], p["qty"], p["entry"]) if p else None

    def open(self, symbol: str, direction: int, qty: float, stop: float) -> Position:
        px = self.price(symbol) * (1 + direction * self.s.slippage)
        self.cash -= qty * px * self.s.fee
        self.positions[symbol] = {"direction": direction, "qty": qty, "entry": px, "stop": stop}
        self._save()
        return Position(direction, qty, px)

    def close(self, symbol: str, price: float | None = None, qty: float | None = None) -> float:
        p = self.positions.pop(symbol, None)
        if not p:
            return 0.0
        px = (price or self.price(symbol)) * (1 - p["direction"] * self.s.slippage)
        pnl = p["direction"] * p["qty"] * (px - p["entry"]) - p["qty"] * px * self.s.fee
        self.cash += pnl
        self._save()
        return pnl

    def set_stop(self, symbol: str, stop: float, qty: float | None = None) -> None:
        if symbol in self.positions:
            self.positions[symbol]["stop"] = stop
            self._save()

    def check_stops(self) -> list[str]:
        """Simulate stop orders. Returns symbols that were stopped out."""
        hit = []
        for sym, p in list(self.positions.items()):
            px = self.price(sym)
            if (p["direction"] == 1 and px <= p["stop"]) or (p["direction"] == -1 and px >= p["stop"]):
                pnl = self.close(sym, px)
                log.info("[PAPER] %s stop hit @ %.4f, pnl %.2f", sym, px, pnl)
                hit.append(sym)
        return hit

    def flatten(self, tracked: dict | None = None):
        for sym in list(self.positions):
            self.close(sym)


class ExchangeBroker:
    """Real orders on Binance (demo or live), with exchange-side stop orders."""

    def __init__(self, settings: Settings, ex: ccxt.Exchange):
        self.s, self.ex = settings, ex
        self.futures = settings.market == "futures"
        ex.load_markets()
        if self.futures:
            lev = max(1, int(settings.leverage_cap))
            for sym in settings.symbols:
                for call in (lambda: ex.set_margin_mode("isolated", sym),
                             lambda: ex.set_leverage(lev, sym)):
                    try:
                        call()
                    except ccxt.BaseError as e:  # "no need to change" etc.
                        log.debug("futures setup %s: %s", sym, e)

    # ---- helpers
    def price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def _cid(self) -> str:
        return "tb" + uuid.uuid4().hex[:20]

    def _min_cost(self, symbol: str) -> float:
        lim = self.ex.market(symbol).get("limits", {}).get("cost", {}) or {}
        return float(lim.get("min") or 5.0)

    def amount(self, symbol: str, qty: float) -> float:
        return float(self.ex.amount_to_precision(symbol, qty))

    # ---- account
    def equity(self) -> float:
        bal = self.ex.fetch_balance()
        if self.futures:
            info = bal.get("info", {}) or {}
            if info.get("totalMarginBalance") is not None:
                return float(info["totalMarginBalance"])
            return float(bal.get("total", {}).get("USDT", 0) or 0)
        total = float(bal.get("total", {}).get("USDT", 0) or 0)
        for sym in self.s.symbols:
            base = self.ex.market(sym)["base"]
            amt = float(bal.get("total", {}).get(base, 0) or 0)
            if amt:
                total += amt * self.price(sym)
        return total

    def position(self, symbol: str) -> Position | None:
        if self.futures:
            for p in self.ex.fetch_positions([symbol]):
                contracts = float(p.get("contracts") or 0)
                if contracts > 0:
                    d = 1 if p.get("side") == "long" else -1
                    return Position(d, contracts, float(p.get("entryPrice") or 0))
            return None
        base = self.ex.market(symbol)["base"]
        amt = float(self.ex.fetch_balance().get("total", {}).get(base, 0) or 0)
        px = self.price(symbol)
        if amt * px >= self._min_cost(symbol):
            return Position(1, amt, 0.0)  # entry price tracked in bot state
        return None

    # ---- orders
    def cancel_all(self, symbol: str) -> None:
        for params in ({}, {"trigger": True}) if self.futures else ({},):
            try:
                self.ex.cancel_all_orders(symbol, params)
            except ccxt.OrderNotFound:
                pass
            except ccxt.BaseError as e:
                log.warning("cancel_all %s %s: %s", symbol, params, e)

    def open(self, symbol: str, direction: int, qty: float, stop: float) -> Position | None:
        qty = self.amount(symbol, qty)
        px = self.price(symbol)
        if qty <= 0 or qty * px < self._min_cost(symbol):
            log.warning("%s order too small (qty=%s, notional=%.2f)", symbol, qty, qty * px)
            return None
        side = "buy" if direction == 1 else "sell"
        filled, fill = execute(self.ex, symbol, side, qty, maker_first=self.s.maker_first,
                               wait=self.s.maker_wait_seconds, min_cost=self._min_cost(symbol))
        if filled <= 0:
            return None
        fill = fill or px
        if not self.futures:  # spot fee may be taken from the bought coin
            free = float(self.ex.fetch_balance().get("free", {}).get(self.ex.market(symbol)["base"], 0) or 0)
            filled = min(filled, free)
        log.info("OPEN %s %s qty=%s @ %.4f", symbol, side, filled, fill)
        self.set_stop(symbol, stop, filled)
        return Position(direction, filled, fill)

    def set_stop(self, symbol: str, stop: float, qty: float | None = None) -> None:
        """Replace the exchange-side stop order. Spot: only for the bot's own `qty`, never the whole wallet."""
        self.cancel_all(symbol)
        pos = self.position(symbol)
        if not pos:
            return
        stop_px = float(self.ex.price_to_precision(symbol, stop))
        if self.futures:
            side = "sell" if pos.direction == 1 else "buy"
            self.ex.create_order(symbol, "market", side, self.amount(symbol, pos.qty), None,
                                 {"stopLossPrice": stop_px, "reduceOnly": True})
        else:
            free = float(self.ex.fetch_balance().get("free", {}).get(self.ex.market(symbol)["base"], 0) or 0)
            qty = self.amount(symbol, min(free, qty if qty else free))
            limit_px = float(self.ex.price_to_precision(symbol, stop * 0.995))
            self.ex.create_order(symbol, "limit", "sell", qty, limit_px, {"stopLossPrice": stop_px})
        log.info("STOP %s set @ %s", symbol, stop_px)

    def close(self, symbol: str, price: float | None = None, qty: float | None = None) -> float:
        """Spot: sells only the bot's tracked `qty` (your other coins are never touched)."""
        self.cancel_all(symbol)
        pos = self.position(symbol)
        if not pos:
            return 0.0
        side = "sell" if pos.direction == 1 else "buy"
        params = {"reduceOnly": True} if self.futures else {}
        if not self.futures:
            free = float(self.ex.fetch_balance().get("free", {}).get(self.ex.market(symbol)["base"], 0) or 0)
            qty = self.amount(symbol, min(free, qty if qty else 0.0))
        else:
            qty = self.amount(symbol, pos.qty)
        if qty > 0:
            self.ex.create_order(symbol, "market", side, qty, None, params)
            log.info("CLOSE %s %s qty=%s", symbol, side, qty)
        return 0.0

    def check_stops(self) -> list[str]:
        return []  # handled by the exchange; detected via reconciliation

    def flatten(self, tracked: dict | None = None):
        for sym in self.s.symbols:
            if self.futures:
                self.close(sym)
            elif tracked and sym in tracked:
                self.close(sym, qty=tracked[sym].get("qty"))


# --------------------------------------------------------------------------- bot
class Bot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.spec = get_strategy(settings.strategy)
        self.ex = make_exchange(settings)
        self.broker = PaperBroker(settings, self.ex) if settings.mode == "paper" else ExchangeBroker(settings, self.ex)
        tag = f"{settings.mode}_{settings.market}"
        self.risk = RiskManager(settings.max_daily_loss, settings.max_drawdown,
                                settings.max_open_positions, STATE_DIR / f"risk_{tag}.json")
        self.state_file = STATE_DIR / f"bot_{tag}.json"
        self.notify = Notifier(prefix=f"[TradingBot {settings.mode}/{settings.market}]")
        self.state = {"last_bar": {}, "trades": {}, "blocked": {}, "summary_day": ""}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))

    @property
    def trades(self) -> dict:
        return self.state["trades"]

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2, default=str))

    def _position(self, sym: str) -> Position | None:
        """Bot-owned position. Spot: only what the bot bought itself (your other coins are ignored)."""
        if self.s.market == "spot" and sym not in self.trades:
            return None
        pos = self.broker.position(sym)
        if pos and self.s.market == "spot":
            tr = self.trades[sym]
            pos = Position(1, min(pos.qty, tr["qty"]), tr["entry"])
        return pos

    # ---- reconciliation
    def reconcile(self):
        """Align saved state with what the exchange actually holds."""
        for sym in self.s.symbols:
            saved = self.trades.get(sym)
            pos = self._position(sym)
            if saved and not pos:
                log.info("%s: position gone (stop hit or closed manually) -> no re-entry %+d until signal resets",
                         sym, saved["direction"])
                self.notify.send(f"🛑 {sym} pozisyonu kapandı (stop veya manuel). Giriş {saved['entry']:.6g}, "
                                 f"stop {saved['stop']:.6g}")
                self.state["blocked"][sym] = saved["direction"]
                self.trades.pop(sym)
            elif pos and not saved and self.s.market == "futures":
                log.warning("%s: found a futures position the bot did not open: %s. "
                            "Adopting it with a fresh ATR stop.", sym, pos)
                df = fetch_recent_closed(self.ex, sym, self.s.timeframe, 300)
                a = float(atr_fn(df).iloc[-1])
                entry = pos.entry or float(df["close"].iloc[-1])
                stop = initial_stop(entry, pos.direction, a, self.s.atr_stop_mult)
                self.trades[sym] = {"direction": pos.direction, "entry": entry, "stop": stop, "qty": pos.qty}
                self.broker.set_stop(sym, stop, pos.qty)
        self._save()

    # ---- per-bar logic
    def on_bar(self, sym: str, df: pd.DataFrame, btc: pd.DataFrame | None = None):
        target = make_target(self.spec.name, df, self.s.allow_short, btc)
        want = int(target.iloc[-1])
        a = float(atr_fn(df).iloc[-1])
        close = float(df["close"].iloc[-1])
        blocked = self.state["blocked"].get(sym, 0)
        if blocked and want != blocked:
            self.state["blocked"].pop(sym, None)
            blocked = 0

        pos = self._position(sym)
        tr = self.trades.get(sym)

        if pos and want != pos.direction:
            pnl = self.broker.close(sym, qty=pos.qty)
            log.info("%s EXIT on signal (want=%+d) pnl=%.2f", sym, want, pnl)
            chg = (close / tr["entry"] - 1) * 100 * pos.direction if tr and tr.get("entry") else 0.0
            self.notify.send(f"✅ {sym} sinyalle kapatıldı @ {close:.6g} ({chg:+.1f}%)")
            self.trades.pop(sym, None)
            pos = None
        elif pos and tr and self.s.trailing:
            new_stop = trail_stop(tr["stop"], pos.direction, close, a, self.s.atr_stop_mult)
            if abs(new_stop - tr["stop"]) / close > 0.001:
                self.broker.set_stop(sym, new_stop, pos.qty)
                tr["stop"] = new_stop
                log.info("%s trail stop -> %.4f", sym, new_stop)

        if not pos and want != 0 and want != blocked:
            equity = self.broker.equity()
            open_n = len(self.trades)
            ok, why = self.risk.can_open(equity, open_n)
            if not ok:
                log.info("%s signal %+d skipped: %s", sym, want, why)
            else:
                px = self.broker.price(sym)
                stop = initial_stop(px, want, a, self.s.atr_stop_mult)
                # leave headroom for fees/slippage; total exposure of all bot positions is capped
                # at equity * leverage_cap (spot: 1x = only the cash you have)
                qty = position_size(equity, px, stop, self.s.risk_per_trade, self.s.leverage_cap * 0.95)
                used = sum(t["qty"] * self.broker.price(k) for k, t in self.trades.items())
                room = max(0.0, equity * self.s.leverage_cap * 0.95 - used)
                qty = min(qty, room / px)
                newpos = self.broker.open(sym, want, qty, stop)
                if newpos:
                    self.trades[sym] = {"direction": want, "entry": newpos.entry, "stop": stop,
                                        "qty": newpos.qty, "opened": datetime.now(timezone.utc).isoformat()}
                    log.info("%s ENTER %+d qty=%.6f entry=%.4f stop=%.4f (risk %.2f%% of %.2f)",
                             sym, want, newpos.qty, newpos.entry, stop, self.s.risk_per_trade * 100, equity)
                    self.notify.send(f"🟢 {sym} {'LONG' if want == 1 else 'SHORT'} açıldı @ {newpos.entry:.6g}, "
                                     f"stop {stop:.6g} ({(stop / newpos.entry - 1) * 100:+.1f}%), "
                                     f"risk %{self.s.risk_per_trade * 100:.2f}")
        self._save()

    def _daily_summary(self, equity: float):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("summary_day") == day:
            return
        first = not self.state.get("summary_day")
        self.state["summary_day"] = day
        self._save()
        if first:
            self.notify.send(f"▶️ Bot başladı. Özsermaye {equity:.2f} USDT, açık pozisyon {len(self.trades)}.")
            return
        lines = [f"📊 Günlük özet {day}: özsermaye {equity:.2f} USDT",
                 f"Zirveden: {(equity / max(self.risk.state.peak_equity, 1e-9) - 1) * 100:+.1f}%",
                 f"Açık pozisyon: {len(self.trades)}"]
        lines += [f"  {k} giriş {v['entry']:.6g} stop {v['stop']:.6g}" for k, v in self.trades.items()]
        self.notify.send("\n".join(lines))

    def _notify_error(self, e: Exception):
        now = time.time()
        if now - getattr(self, "_last_err", 0) > 3600:  # at most one error message per hour
            self._last_err = now
            self.notify.send(f"⚠️ Döngü hatası (tekrar denenecek): {type(e).__name__}: {str(e)[:200]}")

    def flatten(self):
        self.broker.flatten(self.trades)
        self.state["trades"] = {}
        self._save()

    # ---- main loop
    def run(self, poll_seconds: int = 30, once: bool = False):
        log.info("Bot start | mode=%s market=%s strategy=%s symbols=%s tf=%s",
                 self.s.mode, self.s.market, self.s.strategy, self.s.symbols, self.s.timeframe)
        if self.s.mode == "live":
            log.warning("LIVE MODE: REAL MONEY")
        if self.risk.state.halted:
            log.critical("Bot is HALTED (%s). Run `python -m tradingbot reset-halt` after reviewing.",
                         self.risk.state.halt_reason)
            return
        while True:
            if STOP_FILE.exists():
                log.info("STOP file found -> exiting (open positions keep their stop orders).")
                return
            try:
                for sym in self.broker.check_stops():
                    tr = self.trades.pop(sym, None)
                    if tr:
                        self.state["blocked"][sym] = tr["direction"]
                if self.s.mode != "paper":
                    self.reconcile()
                equity = self.broker.equity()
                self.risk.update(equity)
                self._daily_summary(equity)
                if self.risk.state.halted:
                    log.critical("KILL SWITCH: %s -> closing bot positions and stopping.",
                                 self.risk.state.halt_reason)
                    self.notify.send(f"🚨 KILL SWITCH: {self.risk.state.halt_reason}. Pozisyonlar kapatılıyor, "
                                     f"bot durdu.", block=True)
                    self.flatten()
                    return
                btc = (fetch_recent_closed(self.ex, self.s.btc_symbol, self.s.timeframe, 1000)
                       if self.s.btc_filter else None)
                for sym in self.s.symbols:
                    df = fetch_recent_closed(self.ex, sym, self.s.timeframe, 1000)
                    if len(df) < self.spec.warmup:
                        log.warning("%s: not enough candles (%d)", sym, len(df))
                        continue
                    last = str(df.index[-1])
                    if self.state["last_bar"].get(sym) == last:
                        continue
                    self.state["last_bar"][sym] = last
                    log.info("%s closed bar %s close=%.4f | equity=%.2f",
                             sym, last, df["close"].iloc[-1], equity)
                    self.on_bar(sym, df, btc)
            except ccxt.AuthenticationError as e:
                log.critical("Authentication failed, check API keys / permissions: %s", e)
                self.notify.send("❌ API anahtarı reddedildi, bot durdu. Anahtar/izinleri kontrol et.", block=True)
                return
            except ccxt.DDoSProtection as e:
                log.error("Rate limited: %s. Sleeping 5 min.", e)
                time.sleep(300)
            except Exception as e:
                log.exception("Loop error (will retry)")
                self._notify_error(e)
            if once:
                return
            time.sleep(poll_seconds)
