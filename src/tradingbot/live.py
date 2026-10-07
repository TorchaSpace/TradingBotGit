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
  - backup stop: if price is beyond the stop and the exchange stop did not fire (gap through a
    spot stop-limit, rejected order, ...), the bot closes the position at market itself
  - the bot only ever manages positions it opened itself (tracked in state/); anything else in
    the account (your own coins, a carry hedge, manual trades) is ignored
  - every open / close is written to state/journal_<mode>_<market>.csv (see `report`)
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt
import pandas as pd

from .config import PROJECT_ROOT, Settings
from .exchange import fetch_recent_closed, make_exchange
from .execution import MarketQualityError, execute
from .notify import Notifier
from .indicators import atr as atr_fn
from .journal import Journal
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
        return px

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
                exit_px = self.close(sym, px)
                log.info("[PAPER] %s stop hit @ %.4f", sym, exit_px)
                hit.append((sym, exit_px))
        return hit

    def flatten(self, tracked: dict | None = None):
        for sym in list(self.positions):
            self.close(sym)

    def protect(self, symbol: str, stop: float, qty: float) -> None:
        self.set_stop(symbol, stop, qty)


class ExchangeBroker:
    """Real orders on Binance (demo or live), with exchange-side stop orders."""

    def __init__(self, settings: Settings, ex: ccxt.Exchange):
        self.s, self.ex = settings, ex
        self.futures = settings.market == "futures"
        ex.load_markets()
        self.tracked: dict = {}  # bot-owned trades, set by Bot (used for spot equity)
        if self.futures:
            try:  # the bot assumes one-way mode (one net position per symbol)
                ex.set_position_mode(False)
            except ccxt.BaseError as e:
                log.debug("position mode: %s", e)
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
        # spot: free+locked USDT plus ONLY the coins the bot bought itself
        total = float(bal.get("total", {}).get("USDT", 0) or 0)
        for sym, tr in self.tracked.items():
            base = self.ex.market(sym)["base"]
            amt = min(float(bal.get("total", {}).get(base, 0) or 0), float(tr.get("qty", 0)))
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
        if self.s.mode == "live" and self.s.price_check:
            from .pricecheck import default as price_check
            price_check().verify(symbol, px)            # raises MarketQualityError on a bad price
        filled, fill = execute(self.ex, symbol, side, qty, maker_first=self.s.maker_first,
                               wait=self.s.maker_wait_seconds, min_cost=self._min_cost(symbol),
                               guard=(self.s.max_spread, self.s.max_impact))
        if filled <= 0:
            return None
        fill = fill or px
        if not self.futures:  # spot fee may be taken from the bought coin
            free = float(self.ex.fetch_balance().get("free", {}).get(self.ex.market(symbol)["base"], 0) or 0)
            filled = min(filled, free)
        log.info("OPEN %s %s qty=%s @ %.4f", symbol, side, filled, fill)
        return Position(direction, filled, fill)

    def protect(self, symbol: str, stop: float, qty: float) -> None:
        """Place the exchange-side stop; raises if the exchange rejects it (caller handles)."""
        self.set_stop(symbol, stop, qty)

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
        if qty <= 0:
            return 0.0
        o = self.ex.create_order(symbol, "market", side, qty, None, params)
        exit_px = float(o.get("average") or o.get("price") or 0) or self.price(symbol)
        log.info("CLOSE %s %s qty=%s @ %.6g", symbol, side, qty, exit_px)
        return exit_px

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
        self.journal = Journal(STATE_DIR / f"journal_{tag}.csv", STATE_DIR / f"equity_{tag}.csv")
        try:
            from .learner import LiveFilter
            self.learner = LiveFilter(settings)   # inactive unless LEARNER_MODE=filter AND model approved
        except Exception:
            self.learner = None
        self._wake = threading.Event()   # set by the websocket stream when an order changes
        self.stream = None
        self.state = {"last_bar": {}, "trades": {}, "blocked": {}, "summary_day": "", "warned": []}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))
        if isinstance(self.broker, ExchangeBroker):
            self.broker.tracked = self.state["trades"]

    @property
    def trades(self) -> dict:
        return self.state["trades"]

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2, default=str))

    def _position(self, sym: str) -> Position | None:
        """Bot-owned position only. Anything the bot did not open itself is ignored."""
        if sym not in self.trades:
            return None
        pos = self.broker.position(sym)
        tr = self.trades[sym]
        if pos and self.s.market == "spot":
            pos = Position(1, min(pos.qty, tr["qty"]), tr["entry"])
        elif pos and pos.direction != tr["direction"]:
            return None  # the net futures position flipped (e.g. manual trade) -> not ours anymore
        return pos

    def _pnl(self, tr: dict, exit_px: float) -> float:
        """Estimated PnL after fees for a bot trade closed at exit_px."""
        q, e, d = float(tr.get("qty", 0)), float(tr.get("entry", 0)), int(tr.get("direction", 1))
        return d * q * (exit_px - e) - self.s.fee * q * (exit_px + e)

    def _record_close(self, sym: str, tr: dict, exit_px: float, reason: str) -> float:
        pnl = self._pnl(tr, exit_px) if exit_px else 0.0
        self.journal.trade("close", sym, tr.get("direction", 0), tr.get("qty", 0), exit_px,
                           tr.get("stop", 0), reason, pnl, tr.get("entry", 0))
        return pnl

    def _protect(self, sym: str) -> None:
        """Place / re-place the exchange stop for a tracked trade; remember failures for retry."""
        tr = self.trades.get(sym)
        if not tr:
            return
        try:
            self.broker.protect(sym, tr["stop"], tr["qty"])
            if not tr.get("stop_ok", True):
                self.notify.send(f"✅ {sym} stop emri tekrar denendi ve yerleşti @ {tr['stop']:.6g}")
            tr["stop_ok"] = True
        except ccxt.BaseError as e:
            first = tr.get("stop_ok", True)
            tr["stop_ok"] = False
            log.error("%s: stop order rejected (%s). Bot-side stop is active, will retry.", sym, e)
            if first:
                self.notify.send(f"⚠️ {sym} için borsa stop emri reddedildi: {str(e)[:120]}. "
                                 f"Bot kendi stop kontrolünü yapıyor, tekrar denenecek.")
        self._save()

    def backup_stops(self) -> None:
        """Close at market if price is beyond the stop but the position is still open
        (gap through a spot stop-limit, rejected/missing stop order, ...). Exchange modes only."""
        for sym, tr in list(self.trades.items()):
            px = self.broker.price(sym)
            d, stop = tr["direction"], tr["stop"]
            breached = (d == 1 and px < stop * 0.997) or (d == -1 and px > stop * 1.003)
            if not breached and tr.get("stop_ok", True):
                continue
            if not breached:
                self._protect(sym)  # retry a previously rejected stop
                continue
            if not self._position(sym):
                continue  # already closed by the exchange stop -> reconcile handles it
            exit_px = self.broker.close(sym, qty=tr["qty"])
            pnl = self._record_close(sym, tr, exit_px, "backup_stop")
            self.trades.pop(sym, None)
            self.state["blocked"][sym] = d
            log.warning("%s BACKUP STOP: price %.6g beyond stop %.6g -> closed at market (pnl ~%.2f)",
                        sym, px, stop, pnl)
            self.notify.send(f"🛑 {sym} yedek stop: fiyat {px:.6g}, stop {stop:.6g} geçildi, piyasadan "
                             f"kapatıldı (~{pnl:+.2f} USDT)")
            self._save()

    # ---- reconciliation
    def reconcile(self):
        """Align saved state with what the exchange actually holds."""
        for sym in self.s.symbols:
            saved = self.trades.get(sym)
            pos = self._position(sym)
            if saved and not pos:
                pnl = self._record_close(sym, saved, saved["stop"], "stop_or_manual")
                log.info("%s: position gone (stop hit or closed manually, ~%.2f USDT) -> no re-entry %+d "
                         "until signal resets", sym, pnl, saved["direction"])
                self.notify.send(f"🛑 {sym} pozisyonu kapandı (stop veya manuel), tahmini {pnl:+.2f} USDT. "
                                 f"Giriş {saved['entry']:.6g}, stop {saved['stop']:.6g}")
                self.state["blocked"][sym] = saved["direction"]
                self.trades.pop(sym)
            elif not saved and self.s.market == "futures" and sym not in self.state["warned"]:
                other = self.broker.position(sym)
                if other:
                    self.state["warned"].append(sym)
                    log.warning("%s: there is a futures position the bot did not open (%s). It is IGNORED. "
                                "Do not trade the bot's symbols by hand in the same account.", sym, other)
                    self.notify.send(f"ℹ️ {sym}: hesapta botun açmadığı bir futures pozisyonu var, bot ona "
                                     f"dokunmuyor. Bot ile aynı hesapta aynı coinde elle işlem yapma.")
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
            exit_px = self.broker.close(sym, qty=pos.qty)
            pnl = self._record_close(sym, tr, exit_px or close, "signal")
            log.info("%s EXIT on signal (want=%+d) @ %.6g pnl~%.2f", sym, want, exit_px or close, pnl)
            self.notify.send(f"✅ {sym} sinyalle kapatıldı @ {exit_px or close:.6g} ({pnl:+.2f} USDT)")
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
            if ok and self.learner is not None and self.learner.active:
                allow, prob = self.learner.check(df, btc, want)
                if not allow:
                    ok, why = False, f"öğrenen model zayıf buldu (kazanma olasılığı {prob:.0%})"
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
                    # record FIRST so the position is never untracked, then protect it
                    self.trades[sym] = {"direction": want, "entry": newpos.entry, "stop": stop,
                                        "qty": newpos.qty, "opened": datetime.now(timezone.utc).isoformat()}
                    self._save()
                    self.journal.trade("open", sym, want, newpos.qty, newpos.entry, stop, "signal", 0.0,
                                       newpos.entry, equity)
                    self._protect(sym)
                    log.info("%s ENTER %+d qty=%.6f entry=%.4f stop=%.4f (risk %.2f%% of %.2f)",
                             sym, want, newpos.qty, newpos.entry, stop, self.s.risk_per_trade * 100, equity)
                    self.notify.send(f"🟢 {sym} {'LONG' if want == 1 else 'SHORT'} açıldı @ {newpos.entry:.6g}, "
                                     f"stop {stop:.6g} ({(stop / newpos.entry - 1) * 100:+.1f}%), "
                                     f"risk %{self.s.risk_per_trade * 100:.2f}")
        self._save()

    def _last_closed_bar(self) -> str:
        """Open time of the most recent fully closed candle, as stored in state['last_bar']."""
        tf_ms = self.ex.parse_timeframe(self.s.timeframe) * 1000
        now = self.ex.milliseconds()
        ts = (now // tf_ms) * tf_ms - tf_ms
        return str(pd.Timestamp(ts, unit="ms", tz="UTC"))

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

    def flatten(self, reason: str = "flatten"):
        for sym, tr in list(self.trades.items()):
            if not self._position(sym):
                self.trades.pop(sym, None)
                continue
            exit_px = self.broker.close(sym, qty=tr["qty"])
            self._record_close(sym, tr, exit_px, reason)
            self.trades.pop(sym, None)
            self._save()

    # ---- main loop
    def run(self, poll_seconds: int = 30, once: bool = False, stop_event=None):
        """Main loop. `stop_event` (threading.Event) lets the desktop app stop it cleanly."""
        if self.s.mode != "paper" and self.s.user_stream and not once:
            try:
                from .userstream import UserStream
                self.stream = UserStream(self.s, self._on_order_event).start()
            except Exception as e:
                log.warning("Anlık emir bildirimi açılamadı: %s", e)
        try:
            return self._run(poll_seconds, once, stop_event)
        finally:
            if self.stream is not None:
                self.stream.stop()

    def _on_order_event(self, order: dict) -> None:
        log.info("Emir güncellemesi: %s %s %s -> hemen kontrol", order.get("symbol"), order.get("type"),
                 order.get("status"))
        self._wake.set()

    def _run(self, poll_seconds: int, once: bool, stop_event):
        self.status = {"state": "running", "last_loop": None, "equity": None, "error": None,
                       "loops": 0}
        log.info("Bot start | mode=%s market=%s strategy=%s symbols=%s tf=%s",
                 self.s.mode, self.s.market, self.s.strategy, self.s.symbols, self.s.timeframe)
        if self.s.mode == "live":
            log.warning("LIVE MODE: REAL MONEY")
        if self.risk.state.halted:
            log.critical("Bot is HALTED (%s). Run `python -m tradingbot reset-halt` after reviewing.",
                         self.risk.state.halt_reason)
            self.status["state"] = "halted"
            return
        while True:
            if STOP_FILE.exists() or (stop_event is not None and stop_event.is_set()):
                log.info("Stop requested -> exiting (open positions keep their stop orders).")
                self.status["state"] = "stopped"
                return
            try:
                for sym, exit_px in self.broker.check_stops():
                    tr = self.trades.pop(sym, None)
                    if tr:
                        self._record_close(sym, tr, exit_px, "stop")
                        self.state["blocked"][sym] = tr["direction"]
                        self.notify.send(f"🛑 {sym} stop @ {exit_px:.6g}")
                if self.s.mode != "paper":
                    self.reconcile()
                    self.backup_stops()
                equity = self.broker.equity()
                self.status.update(equity=equity, last_loop=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"), error=None, loops=self.status["loops"] + 1)
                self.risk.update(equity)
                self.journal.equity(equity, len(self.trades))
                self._daily_summary(equity)
                if self.risk.state.halted:
                    log.critical("KILL SWITCH: %s -> closing bot positions and stopping.",
                                 self.risk.state.halt_reason)
                    self.notify.send(f"🚨 KILL SWITCH: {self.risk.state.halt_reason}. Pozisyonlar kapatılıyor, "
                                     f"bot durdu.", block=True)
                    self.flatten("kill_switch")
                    self.status["state"] = "halted"
                    return
                expected = self._last_closed_bar()
                pending = [x for x in self.s.symbols if self.state["last_bar"].get(x) != expected]
                btc = (fetch_recent_closed(self.ex, self.s.btc_symbol, self.s.timeframe, 1000)
                       if self.s.btc_filter and pending else None)
                for sym in pending:
                    try:
                        df = fetch_recent_closed(self.ex, sym, self.s.timeframe, 1000)
                        if len(df) < self.spec.warmup:
                            log.warning("%s: not enough candles (%d)", sym, len(df))
                            continue
                        last = str(df.index[-1])
                        if self.state["last_bar"].get(sym) == last:
                            continue
                        log.info("%s closed bar %s close=%.4f | equity=%.2f",
                                 sym, last, df["close"].iloc[-1], equity)
                        self.on_bar(sym, df, btc)
                        self.state["last_bar"][sym] = last  # only after success -> retried on error
                        self._save()
                    except (ccxt.AuthenticationError, ccxt.DDoSProtection):
                        raise
                    except MarketQualityError as e:  # nothing was sent; retried on the next loop
                        log.warning("%s: giriş ertelendi: %s", sym, e)
                        self.status["note"] = f"{sym}: giriş ertelendi ({e})"
                    except Exception as e:  # one bad symbol must not block the others
                        log.exception("%s: bar processing failed (will retry next loop)", sym)
                        self._notify_error(e)
            except ccxt.AuthenticationError as e:
                log.critical("Authentication failed, check API keys / permissions: %s", e)
                self.notify.send("❌ API anahtarı reddedildi, bot durdu. Anahtar/izinleri kontrol et.", block=True)
                self.status.update(state="error", error=f"API anahtarı reddedildi: {e}")
                return
            except ccxt.DDoSProtection as e:
                log.error("Rate limited: %s. Sleeping 5 min.", e)
                self.status["error"] = f"Rate limit: {e}"
                if self._sleep(300, stop_event):
                    continue
            except Exception as e:
                log.exception("Loop error (will retry)")
                self.status["error"] = f"{type(e).__name__}: {e}"
                self._notify_error(e)
            if once:
                return
            self._sleep(poll_seconds, stop_event)

    def _sleep(self, seconds: float, stop_event=None) -> bool:
        """Wait `seconds`; returns True if stopping. An order update from the websocket ends the wait early."""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return False
            if stop_event is not None and stop_event.is_set():
                return True
            if self._wake.wait(min(1.0, left)):
                self._wake.clear()
                return bool(stop_event is not None and stop_event.is_set())
