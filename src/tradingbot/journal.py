"""Append-only CSV journal of every trade and a periodic equity log (used by `report`)."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

TRADE_FIELDS = ["time", "action", "symbol", "direction", "qty", "price", "stop", "reason",
                "pnl_est", "entry", "equity"]
EQUITY_FIELDS = ["time", "equity", "open_positions"]


def _append(path: Path, fields: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


class Journal:
    def __init__(self, trades_path: Path, equity_path: Path, equity_every_s: int = 3600):
        self.trades_path, self.equity_path = Path(trades_path), Path(equity_path)
        self.every = equity_every_s
        self._last_eq = 0.0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def trade(self, action: str, symbol: str, direction: int, qty: float, price: float, stop: float,
              reason: str, pnl: float = 0.0, entry: float = 0.0, equity: float | None = None) -> None:
        _append(self.trades_path, TRADE_FIELDS, {
            "time": self._now(), "action": action, "symbol": symbol, "direction": int(direction),
            "qty": f"{float(qty):.10g}", "price": f"{float(price or 0):.10g}", "stop": f"{float(stop or 0):.10g}",
            "reason": reason, "pnl_est": f"{float(pnl):.4f}", "entry": f"{float(entry or 0):.10g}",
            "equity": "" if equity is None else f"{float(equity):.2f}"})

    def equity(self, equity: float, open_positions: int, force: bool = False) -> None:
        now = datetime.now(timezone.utc).timestamp()
        if not force and now - self._last_eq < self.every:
            return
        self._last_eq = now
        _append(self.equity_path, EQUITY_FIELDS,
                {"time": self._now(), "equity": f"{equity:.2f}", "open_positions": open_positions})
