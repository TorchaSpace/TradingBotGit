"""Bar-by-bar backtest engine.

Execution model (conservative, no look-ahead):
- Signal decided on bar t close -> order filled at bar t+1 OPEN, with slippage.
- ATR stop placed at entry; checked against each bar's high/low. If a bar gaps through the
  stop, the fill is the (worse) open price.
- If the stop and an exit signal would both apply, the stop is assumed to hit first (worst case).
- Trailing stop updated at each bar close.
- Fees charged on both sides; futures pay an assumed funding cost every bar (conservative).
- After a stop-out the same direction is not re-entered until the strategy's signal resets.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import atr as atr_fn
from .risk import initial_stop, position_size, trail_stop


@dataclass
class BacktestParams:
    start_equity: float = 1000.0
    fee: float = 0.001
    slippage: float = 0.0005
    risk_per_trade: float = 0.01
    atr_period: int = 14
    atr_mult: float = 2.0
    trailing: bool = True
    leverage_cap: float = 1.0
    allow_short: bool = False
    funding_rate_8h: float = 0.0  # e.g. 0.0001 for futures
    bar_hours: float = 4.0


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def run_backtest(df: pd.DataFrame, target: pd.Series, p: BacktestParams) -> BacktestResult:
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    a = atr_fn(df, p.atr_period).to_numpy(float)
    tgt = target.reindex(df.index).fillna(0).astype(int).to_numpy()
    if not p.allow_short:
        tgt = np.where(tgt < 0, 0, tgt)

    n = len(df)
    cash = p.start_equity          # realized equity
    pos_dir, qty, entry, stop = 0, 0.0, 0.0, 0.0
    entry_time = None
    entry_fee = 0.0
    blocked = 0                    # direction blocked after a stop-out
    funding_per_bar = p.funding_rate_8h * p.bar_hours / 8.0
    eq = np.empty(n)
    trades: list[dict] = []

    def close_pos(i: int, price: float, reason: str):
        nonlocal cash, pos_dir, qty, entry, stop, entry_time, entry_fee
        fee = qty * price * p.fee
        pnl = pos_dir * qty * (price - entry)
        cash += pnl - fee
        trades.append({
            "entry_time": entry_time, "exit_time": df.index[i], "direction": pos_dir,
            "entry": entry, "exit": price, "qty": qty,
            "pnl": pnl - fee - entry_fee, "return_pct": (pos_dir * (price - entry) / entry) * 100,
            "reason": reason,
        })
        pos_dir, qty, entry, stop, entry_time, entry_fee = 0, 0.0, 0.0, 0.0, None, 0.0

    eq[0] = cash
    for i in range(1, n):
        want = tgt[i - 1]
        if blocked and want != blocked:
            blocked = 0

        # 1) act at open on yesterday's signal
        if pos_dir != 0 and want != pos_dir:
            close_pos(i, o[i] * (1 - pos_dir * p.slippage), "signal")
        if pos_dir == 0 and want != 0 and want != blocked and not np.isnan(a[i - 1]) and cash > 0:
            px = o[i] * (1 + want * p.slippage)
            st = initial_stop(px, want, a[i - 1], p.atr_mult)
            q = position_size(cash, px, st, p.risk_per_trade, p.leverage_cap)
            if q > 0:
                pos_dir, qty, entry, stop, entry_time = want, q, px, st, df.index[i]
                entry_fee = q * px * p.fee
                cash -= entry_fee

        # 2) stop check inside the bar
        if pos_dir == 1 and l[i] <= stop:
            close_pos(i, min(o[i], stop) * (1 - p.slippage), "stop")
            blocked = 1
        elif pos_dir == -1 and h[i] >= stop:
            close_pos(i, max(o[i], stop) * (1 + p.slippage), "stop")
            blocked = -1

        # 3) funding + trailing at close
        if pos_dir != 0:
            cash -= qty * c[i] * funding_per_bar
            if p.trailing and not np.isnan(a[i]):
                stop = trail_stop(stop, pos_dir, c[i], a[i], p.atr_mult)

        eq[i] = cash + (pos_dir * qty * (c[i] - entry) if pos_dir else 0.0)
        if eq[i] <= 0:  # account blown
            if pos_dir:
                close_pos(i, c[i], "liquidated")
            eq[i:] = max(cash, 0.0)
            break

    if pos_dir != 0:
        close_pos(n - 1, c[-1], "end")
        eq[-1] = cash

    equity = pd.Series(eq, index=df.index, name="equity")
    trades_df = pd.DataFrame(trades)
    res = BacktestResult(equity, trades_df)
    res.metrics = compute_metrics(equity, trades_df, p.bar_hours, df)
    return res


def compute_metrics(equity: pd.Series, trades: pd.DataFrame, bar_hours: float,
                    df: pd.DataFrame | None = None) -> dict:
    bars_per_year = 365 * 24 / bar_hours
    rets = equity.pct_change().dropna()
    years = max(len(equity) / bars_per_year, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1.0
    dd = equity / equity.cummax() - 1
    sharpe = (rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 0 else 0.0
    downside = rets[rets < 0].std()
    sortino = (rets.mean() / downside * np.sqrt(bars_per_year)) if downside and downside > 0 else 0.0
    m = {
        "total_return_%": total * 100,
        "cagr_%": cagr * 100,
        "max_drawdown_%": dd.min() * 100,
        "sharpe": sharpe,
        "sortino": sortino,
        "trades": int(len(trades)),
        "win_rate_%": 0.0,
        "profit_factor": 0.0,
        "avg_win_loss_ratio": 0.0,
        "exposure_%": 0.0,
    }
    if len(trades):
        wins = trades.loc[trades.pnl > 0, "pnl"]
        losses = trades.loc[trades.pnl <= 0, "pnl"]
        m["win_rate_%"] = len(wins) / len(trades) * 100
        m["profit_factor"] = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
        if len(wins) and len(losses) and losses.mean() < 0:
            m["avg_win_loss_ratio"] = wins.mean() / -losses.mean()
        held = sum((t.exit_time - t.entry_time).total_seconds() for t in trades.itertuples())
        span = (equity.index[-1] - equity.index[0]).total_seconds()
        m["exposure_%"] = held / span * 100 if span > 0 else 0.0
    if df is not None and len(df) > 1:
        m["buy_hold_%"] = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    return m
