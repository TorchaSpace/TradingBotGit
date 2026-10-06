"""Order execution helpers shared by the trend bot and the carry engine.

maker_first: place a post-only limit order at the best bid/ask (maker fee, no spread paid),
wait up to `wait` seconds, cancel what is left and finish with a market order. Used for
ENTRIES only; exits and stops always use market orders so risk is never delayed.
"""
from __future__ import annotations

import logging
import time
import uuid

import ccxt

log = logging.getLogger(__name__)


def client_id() -> str:
    return "tb" + uuid.uuid4().hex[:20]


def execute(ex: ccxt.Exchange, symbol: str, side: str, qty: float, *, maker_first: bool = False,
            wait: int = 45, params: dict | None = None, min_cost: float = 5.0,
            sleep=time.sleep) -> tuple[float, float]:
    """Returns (filled_qty, average_price). Raises only on market-order failure."""
    params = dict(params or {})
    filled, cost = 0.0, 0.0
    if maker_first:
        try:
            book = ex.fetch_order_book(symbol, 5)
            px = float(book["bids"][0][0] if side == "buy" else book["asks"][0][0])
            o = ex.create_order(symbol, "limit", side, qty, px,
                                {**params, "postOnly": True, "newClientOrderId": client_id()})
            deadline = time.time() + wait
            while o.get("status") == "open" and time.time() < deadline:
                sleep(3)
                o = ex.fetch_order(o["id"], symbol)
            if o.get("status") == "open":
                try:
                    ex.cancel_order(o["id"], symbol)
                except ccxt.OrderNotFound:
                    pass
                o = ex.fetch_order(o["id"], symbol)
            filled = float(o.get("filled") or 0.0)
            cost = filled * float(o.get("average") or px)
            log.info("maker %s %s: filled %s of %s @ %s", side, symbol, filled, qty, px)
        except ccxt.BaseError as e:  # post-only rejected, book empty, ... -> market below
            log.info("maker-first skipped for %s: %s", symbol, e)
    try:
        rest = float(ex.amount_to_precision(symbol, qty - filled)) if qty - filled > 0 else 0.0
    except ccxt.BaseError:  # remainder below the exchange's minimum amount
        rest = 0.0
    if rest > 0:
        ref = float(ex.fetch_ticker(symbol)["last"])
        if rest * ref >= min_cost or filled == 0:
            o = ex.create_order(symbol, "market", side, rest, None, {**params, "newClientOrderId": client_id()})
            f = float(o.get("filled") or rest)
            filled += f
            cost += f * float(o.get("average") or o.get("price") or ref)
    avg = cost / filled if filled else 0.0
    return filled, avg
