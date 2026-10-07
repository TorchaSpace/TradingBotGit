"""Funding carry: long spot + short USDⓈ-M perpetual of the same coin, same quantity.

IMPORTANT: run it in a separate Binance sub-account from a FUTURES trend bot. Futures positions
are netted per symbol, so the carry short and a trend position on the same coin would cancel out.

Price moves cancel out (delta-neutral); the position earns the funding that perp shorts receive
when the market is bullish. It is a different return source from the trend bot (daily return
correlation ~0.1), so it smooths the combined result.

Backtest (23 coins, 2021-2024): ~12%/yr, max drawdown < 1%. BUT funding collapsed in 2025-26:
only ~0.5%/yr on held-out data. The engine simply waits in USDT when funding is low.

Risks that the backtest does not fully capture:
  - the short leg can be liquidated if the price spikes and margin is not topped up
    (CARRY_LEVERAGE <= 3; the engine closes a pair when the short has lost 50% of its margin)
  - spot and perp prices can drift apart (basis) for a while
  - needs USDT in BOTH the spot and the USDⓈ-M futures wallet
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd

from .backtest import compute_metrics
from .config import PROJECT_ROOT, Settings
from .data import funding_8h
from .execution import check_market_quality, execute
from .notify import Notifier

log = logging.getLogger(__name__)
STATE_DIR = PROJECT_ROOT / "state"
STOP_FILE = PROJECT_ROOT / "STOP"
ROUND_TRIP_COST = 0.004  # spot 0.1% x2 + perp 0.05% x2 + slippage


# --------------------------------------------------------------------------- signal
def carry_signal(funding_8h_series: pd.Series, lookback: int) -> float:
    """Average funding over the last `lookback` completed 8h windows (NaN if not enough data)."""
    f = funding_8h_series.dropna()
    return float(f.iloc[-lookback:].mean()) if len(f) >= lookback else float("nan")


def select(signals: dict[str, float], held: set[str], slots: int, enter: float, exit_: float
           ) -> tuple[set[str], list[str], list[str]]:
    """Hysteresis selection. Returns (new_held, to_open, to_close)."""
    to_close = [b for b in held if not (signals.get(b, np.nan) >= exit_)]
    keep = held - set(to_close)
    cands = sorted(((v, b) for b, v in signals.items() if v > enter and b not in keep), reverse=True)
    to_open = [b for _, b in cands[: max(0, slots - len(keep))]]
    return keep | set(to_open), to_open, to_close


# --------------------------------------------------------------------------- backtest
@dataclass
class CarryParams:
    slots: int = 5
    leverage: float = 2.0
    lookback: int = 9
    enter: float = 0.0001
    exit: float = 0.0
    cost: float = ROUND_TRIP_COST


def carry_backtest(funding: dict[str, pd.Series], spot: dict[str, pd.Series], perp: dict[str, pd.Series],
                   p: CarryParams, start: str | None = None, end: str | None = None):
    """funding: 8h sums labelled by window end; spot/perp: prices at 8h boundaries (same labels).
    Capital per slot A buys spot notional N = A / (1 + 1/leverage); the rest is perp margin."""
    F = pd.DataFrame(funding).sort_index()
    S = pd.DataFrame(spot).reindex(F.index).pct_change(fill_method=None)
    P = pd.DataFrame(perp).reindex(F.index).pct_change(fill_method=None)
    trail = F.rolling(p.lookback, min_periods=p.lookback).mean()
    idx = F.index
    if start:
        idx = idx[idx >= pd.Timestamp(start, tz="UTC")]
    if end:
        idx = idx[idx < pd.Timestamp(end, tz="UTC")]
    nf = 1 / (1 + 1 / p.leverage)
    held: set[str] = set()
    rets, log_rows = [], []
    for t in idx:
        r = 0.0
        for b in held:
            s, q, f = S.at[t, b], P.at[t, b], F.at[t, b]
            if np.isnan(s) or np.isnan(q):
                continue
            r += nf * ((0.0 if np.isnan(f) else f) + s - q)
        r /= p.slots
        sig = {b: trail.at[t, b] for b in F.columns if not np.isnan(S.at[t, b])}
        held, opened, closed = select(sig, held, p.slots, p.enter, p.exit)
        r -= nf * p.cost / 2 / p.slots * (len(opened) + len(closed))
        rets.append(r)
        if opened or closed:
            log_rows.append({"time": t, "opened": ",".join(opened), "closed": ",".join(closed),
                             "held": ",".join(sorted(held))})
    R = pd.Series(rets, index=idx)
    equity = (1 + R).cumprod() * 1000
    return equity, pd.DataFrame(log_rows), compute_metrics(equity, pd.DataFrame(), 8)


def prices_8h(close_4h: pd.Series) -> pd.Series:
    """4h candle closes -> price at each 8h boundary (a 4h candle opening at T closes at T+4h)."""
    s = close_4h.copy()
    s.index = s.index + pd.Timedelta(hours=4)
    return s[s.index.hour % 8 == 0]


# --------------------------------------------------------------------------- live engine
class CarryEngine:
    """Runs the carry strategy. MODE=paper simulates with public data; demo/live trade for real."""

    def __init__(self, s: Settings):
        if s.carry_capital <= 0:
            raise ValueError("CARRY_CAPITAL must be > 0 (USDT reserved for the carry engine).")
        self.s = s
        self.notify = Notifier(prefix=f"[Carry {s.mode}]")
        cfg = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True}}
        if s.mode in ("demo", "live"):
            cfg.update(apiKey=s.api_key, secret=s.api_secret)
        self.spot, self.fut = ccxt.binance(dict(cfg)), ccxt.binanceusdm(dict(cfg))
        if s.mode == "demo":
            self.spot.enable_demo_trading(True)
            self.fut.enable_demo_trading(True)
        self.state_file = STATE_DIR / f"carry_{s.mode}.json"
        self.state = {"pairs": {}, "last_window": "", "paper_cash": s.carry_capital, "paper_funding": 0.0}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2, default=str))

    @property
    def pairs(self) -> dict:
        return self.state["pairs"]

    # ---- data
    def funding_signal(self, base: str) -> float:
        since = self.fut.milliseconds() - (self.s.carry_lookback + 3) * 8 * 3600 * 1000
        rows = self.fut.fetch_funding_rate_history(f"{base}/USDT:USDT", since=since, limit=100)
        if not rows:
            return float("nan")
        f = pd.Series([r["fundingRate"] for r in rows],
                      index=pd.to_datetime([r["timestamp"] for r in rows], unit="ms", utc=True))
        return carry_signal(funding_8h(f), self.s.carry_lookback)

    def price(self, base: str) -> float:
        return float(self.spot.fetch_ticker(f"{base}/USDT")["last"])

    # ---- trading
    def open_pair(self, base: str) -> None:
        slot = self.s.carry_capital / self.s.carry_slots
        notional = slot / (1 + 1 / self.s.carry_leverage)
        px = self.price(base)
        ssym, fsym = f"{base}/USDT", f"{base}/USDT:USDT"
        if self.s.mode == "paper":
            qty = notional / px
            self.pairs[base] = {"qty": qty, "spot_entry": px * 1.0005, "perp_entry": px * 0.9995,
                                "opened": datetime.now(timezone.utc).isoformat()}
            self.state["paper_cash"] -= notional * ROUND_TRIP_COST / 2
        else:
            self.spot.load_markets()
            self.fut.load_markets()
            # quantity both markets accept
            qty = float(self.fut.amount_to_precision(fsym, float(self.spot.amount_to_precision(ssym, notional / px))))
            try:
                self.fut.set_margin_mode("isolated", fsym)
            except ccxt.BaseError:
                pass
            try:
                self.fut.set_leverage(int(self.s.carry_leverage), fsym)
            except ccxt.BaseError:
                pass
            # 0) both order books must be deep / tight enough before anything is sent
            check_market_quality(self.fut, fsym, "sell", qty, self.s.max_spread, self.s.max_impact)
            check_market_quality(self.spot, ssym, "buy", qty, self.s.max_spread, self.s.max_impact)
            # 1) short leg FIRST: if it fails nothing is bought and we are not exposed
            fq, fp = execute(self.fut, fsym, "sell", qty, maker_first=self.s.maker_first,
                             wait=self.s.maker_wait_seconds)
            if fq <= 0:
                raise ccxt.ExchangeError(f"perp short for {base} did not fill")
            # 2) spot leg; if it fails, undo the short so we are never left unhedged
            try:
                sq, sp = execute(self.spot, ssym, "buy", fq, maker_first=self.s.maker_first,
                                 wait=self.s.maker_wait_seconds)
                if sq <= 0:
                    raise ccxt.ExchangeError(f"spot buy for {base} did not fill")
            except Exception:
                log.error("CARRY %s: spot leg failed -> closing the short again", base)
                self.fut.create_order(fsym, "market", "buy", fq, None, {"reduceOnly": True})
                raise
            if sq < fq * 0.98:  # partial spot fill -> trim the short to match
                trim = float(self.fut.amount_to_precision(fsym, fq - sq))
                if trim > 0:
                    self.fut.create_order(fsym, "market", "buy", trim, None, {"reduceOnly": True})
                    fq -= trim
            self.pairs[base] = {"qty": fq, "spot_qty": sq, "spot_entry": sp, "perp_entry": fp,
                                "opened": datetime.now(timezone.utc).isoformat()}
        log.info("CARRY OPEN %s qty=%.6g", base, self.pairs[base]["qty"])
        self.notify.send(f"🔵 Carry açıldı: {base} (spot long + perp short), ~{notional:.0f} USDT")
        self._save()

    def close_pair(self, base: str, reason: str) -> None:
        pr = self.pairs.get(base)
        if not pr:
            return
        if self.s.mode == "paper":
            px = self.price(base)
            q = pr["qty"]
            pnl = q * (px * 0.9995 - pr["spot_entry"]) - q * (px * 1.0005 - pr["perp_entry"])
            self.state["paper_cash"] += pnl - q * px * ROUND_TRIP_COST / 2
        else:
            fsym, ssym = f"{base}/USDT:USDT", f"{base}/USDT"
            # close the SHORT first (it is the leg that can be liquidated), then sell the spot coins.
            # If the short close fails we raise and keep the pair recorded -> retried next loop.
            if not pr.get("short_closed"):
                try:
                    self.fut.create_order(fsym, "market", "buy", pr["qty"], None, {"reduceOnly": True})
                except ccxt.BaseError as e:
                    if "reduceonly" not in str(e).lower() and "-2022" not in str(e):
                        raise
                    log.warning("CARRY %s: no short left to close (%s)", base, e)
                pr["short_closed"] = True
                self._save()
            free = float(self.spot.fetch_balance().get("free", {}).get(base, 0) or 0)
            q = float(self.spot.amount_to_precision(ssym, min(free, pr.get("spot_qty", pr["qty"]))))
            if q > 0:
                self.spot.create_order(ssym, "market", "sell", q)
        self.pairs.pop(base)
        log.info("CARRY CLOSE %s (%s)", base, reason)
        self.notify.send(f"⚪ Carry kapatıldı: {base} ({reason})")
        self._save()

    def safety_check(self) -> None:
        """Close a pair if its short leg has lost >= 50% of margin or a leg disappeared."""
        if self.s.mode == "paper" or not self.pairs:
            return
        syms = [f"{b}/USDT:USDT" for b in self.pairs]
        positions = {p["symbol"]: p for p in self.fut.fetch_positions(syms) if float(p.get("contracts") or 0) > 0}
        for b in list(self.pairs):
            if self.pairs[b].get("short_closed"):
                self.close_pair(b, "finishing close")
                continue
            p = positions.get(f"{b}/USDT:USDT")
            if p is None:
                log.error("CARRY %s: short leg missing (liquidated or closed manually) -> selling spot", b)
                self.notify.send(f"🚨 Carry {b}: short taraf yok (likidasyon/manuel). Spot satılıyor.", block=True)
                pr = self.pairs.pop(b)
                free = float(self.spot.fetch_balance().get("free", {}).get(b, 0) or 0)
                q = float(self.spot.amount_to_precision(f"{b}/USDT", min(free, pr.get("spot_qty", pr["qty"]))))
                if q > 0:
                    self.spot.create_order(f"{b}/USDT", "market", "sell", q)
                self._save()
                continue
            margin = float(p.get("initialMargin") or p.get("collateral") or 0)
            upnl = float(p.get("unrealizedPnl") or 0)
            if margin > 0 and upnl <= -0.5 * margin:
                self.close_pair(b, f"short margin {upnl / margin:.0%}")

    def paper_accrue(self, window_end: pd.Timestamp) -> None:
        for b, pr in self.pairs.items():
            since = int((window_end - pd.Timedelta(hours=8)).timestamp() * 1000)
            rows = self.fut.fetch_funding_rate_history(f"{b}/USDT:USDT", since=since, limit=20)
            f = sum(r["fundingRate"] for r in rows if since < r["timestamp"] <= window_end.timestamp() * 1000 + 60000)
            gain = pr["qty"] * self.price(b) * f
            self.state["paper_cash"] += gain
            self.state["paper_funding"] += gain

    # ---- main loop
    def step(self) -> None:
        now = pd.Timestamp.now(tz="UTC")
        window = now.floor("8h")
        if str(window) == self.state["last_window"] or now - window < pd.Timedelta(minutes=2):
            return  # act once per funding window, shortly after settlement
        if self.s.mode == "paper" and self.state["last_window"]:
            self.paper_accrue(window)
        self.state["last_window"] = str(window)
        sig = {}
        for b in self.s.carry_symbols:
            try:
                sig[b] = self.funding_signal(b)
            except ccxt.BaseError as e:
                log.warning("funding %s: %s", b, e)
        held, to_open, to_close = select(sig, set(self.pairs), self.s.carry_slots,
                                         self.s.carry_enter, self.s.carry_exit)
        for b in to_close:
            try:
                self.close_pair(b, f"funding avg {sig.get(b, float('nan')):.5f}")
            except ccxt.BaseError as e:
                log.error("carry close %s failed (will retry): %s", b, e)
                self.notify.send(f"⚠️ Carry {b} kapatılamadı, tekrar denenecek: {str(e)[:150]}")
        for b in to_open:
            try:
                self.open_pair(b)
            except ccxt.BaseError as e:
                log.error("carry open %s failed: %s", b, e)
                self.notify.send(f"⚠️ Carry {b} açılamadı: {str(e)[:150]}")
        top = sorted(((v, b) for b, v in sig.items() if not np.isnan(v)), reverse=True)[:5]
        log.info("CARRY window %s | held=%s | top avg funding: %s", window, sorted(self.pairs),
                 ", ".join(f"{b} {v * 100:.4f}%" for v, b in top))
        self._save()

    def run(self, poll_seconds: int = 120, once: bool = False, stop_event=None):
        self.status = {"state": "running", "last_loop": None, "error": None}
        log.info("Carry engine start | mode=%s capital=%.0f slots=%d lev=%.1f", self.s.mode,
                 self.s.carry_capital, self.s.carry_slots, self.s.carry_leverage)
        self.notify.send(f"▶️ Carry motoru başladı, sermaye {self.s.carry_capital:.0f} USDT")
        while True:
            if STOP_FILE.exists() or (stop_event is not None and stop_event.is_set()):
                log.info("Stop requested -> exiting (pairs stay open, they are hedged).")
                self.status["state"] = "stopped"
                return
            try:
                self.safety_check()
                self.step()
                self.status.update(last_loop=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   error=None)
            except ccxt.AuthenticationError as e:
                log.critical("Authentication failed: %s", e)
                self.notify.send("❌ Carry: API anahtarı reddedildi, durdu.", block=True)
                self.status.update(state="error", error=f"API anahtarı reddedildi: {e}")
                return
            except Exception as e:
                log.exception("carry loop error (will retry)")
                self.status["error"] = f"{type(e).__name__}: {e}"
            if once:
                return
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def flatten(self):
        for b in list(self.pairs):
            self.close_pair(b, "manual flatten")
