"""Risk management shared by backtest and live bot."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def position_size(equity: float, entry: float, stop: float, risk_per_trade: float,
                  leverage_cap: float) -> float:
    """Quantity so that hitting the stop loses `risk_per_trade` of equity, capped by leverage."""
    dist = abs(entry - stop)
    if equity <= 0 or entry <= 0 or dist <= 0:
        return 0.0
    qty_risk = equity * risk_per_trade / dist
    qty_cap = equity * leverage_cap / entry
    return max(0.0, min(qty_risk, qty_cap))


def initial_stop(entry: float, direction: int, atr_value: float, mult: float) -> float:
    return entry - direction * atr_value * mult


def trail_stop(stop: float, direction: int, close: float, atr_value: float, mult: float) -> float:
    """Chandelier-style trailing stop: only ever moves in the trade's favour."""
    candidate = close - direction * atr_value * mult
    return max(stop, candidate) if direction == 1 else min(stop, candidate)


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""


@dataclass
class RiskManager:
    """Daily-loss and max-drawdown guard. State persists to disk so restarts don't reset limits."""
    max_daily_loss: float
    max_drawdown: float
    max_open_positions: int
    state_file: Path | None = None
    state: RiskState = field(default_factory=RiskState)

    def __post_init__(self):
        if self.state_file and Path(self.state_file).exists():
            try:
                self.state = RiskState(**json.loads(Path(self.state_file).read_text()))
            except Exception:  # corrupt file -> start fresh but log it
                log.exception("Could not read risk state, starting fresh")

    def _save(self):
        if self.state_file:
            Path(self.state_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self.state_file).write_text(json.dumps(asdict(self.state), indent=2))

    def update(self, equity: float, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        s = self.state
        if s.day != day:
            s.day, s.day_start_equity = day, equity
        s.peak_equity = max(s.peak_equity, equity)
        if s.peak_equity > 0 and equity <= s.peak_equity * (1 - self.max_drawdown):
            s.halted = True
            s.halt_reason = (f"Max drawdown hit: equity {equity:.2f} <= "
                             f"{(1 - self.max_drawdown) * 100:.0f}% of peak {s.peak_equity:.2f}")
        self._save()

    def daily_loss_hit(self, equity: float) -> bool:
        s = self.state
        return s.day_start_equity > 0 and equity <= s.day_start_equity * (1 - self.max_daily_loss)

    def can_open(self, equity: float, open_positions: int) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"HALTED: {self.state.halt_reason}"
        if self.daily_loss_hit(equity):
            return False, "daily loss limit reached"
        if open_positions >= self.max_open_positions:
            return False, "max open positions reached"
        return True, ""

    def reset_halt(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.peak_equity = 0.0
        self._save()
