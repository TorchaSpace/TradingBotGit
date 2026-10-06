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
    block_after_stop: bool = True
    take_profit_atr: float = 0.0  # 0 = no take-profit; else exit at entry +/- N*ATR
    breakeven_atr: float = 0.0    # 0 = off; move stop to entry once price moved N*ATR in favour
    dd_risk_cut: float = 0.0      # 0 = off; halve risk per trade while equity is this far below its peak
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
            blocked = 1 if p.block_after_stop else 0
        elif pos_dir == -1 and h[i] >= stop:
            close_pos(i, max(o[i], stop) * (1 + p.slippage), "stop")
            blocked = -1 if p.block_after_stop else 0

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


def run_portfolio(dfs: dict[str, pd.DataFrame], targets: dict[str, pd.Series], p: BacktestParams,
                  max_positions: int = 4,
                  risk_scale: dict[str, pd.Series] | None = None) -> BacktestResult:
    """Multi-symbol backtest with ONE shared account, like the live bot.

    - risk per trade is a fraction of the TOTAL equity at entry
    - total notional is capped at equity * leverage_cap (spot: 1x, i.e. cash only)
    - at most `max_positions` open at once; symbols are processed in dict order
    Same fill/stop rules as run_backtest.
    """
    idx = sorted(set().union(*[d.index for d in dfs.values()]))
    idx = pd.DatetimeIndex(idx)
    syms = list(dfs)
    al = {s: dfs[s].reindex(idx) for s in syms}
    A = {s: atr_fn(dfs[s], p.atr_period).reindex(idx).to_numpy(float) for s in syms}
    O = {s: al[s]["open"].to_numpy(float) for s in syms}
    H = {s: al[s]["high"].to_numpy(float) for s in syms}
    L = {s: al[s]["low"].to_numpy(float) for s in syms}
    C = {s: al[s]["close"].ffill().to_numpy(float) for s in syms}
    T = {}
    for s in syms:
        t = targets[s].reindex(idx).fillna(0).astype(int).to_numpy()
        T[s] = np.where(t < 0, 0, t) if not p.allow_short else t

    R = {s: (risk_scale[s].reindex(idx).fillna(1.0).to_numpy(float) if risk_scale and s in risk_scale
             else np.ones(len(idx))) for s in syms}
    cash = p.start_equity
    pos: dict[str, dict] = {}
    blocked = {s: 0 for s in syms}
    funding_per_bar = p.funding_rate_8h * p.bar_hours / 8.0
    eq = np.empty(len(idx))
    trades: list[dict] = []

    def close(s, i, price, reason):
        nonlocal cash
        q = pos.pop(s)
        fee = q["qty"] * price * p.fee
        pnl = q["dir"] * q["qty"] * (price - q["entry"])
        cash += pnl - fee
        trades.append({"symbol": s, "entry_time": q["time"], "exit_time": idx[i], "direction": q["dir"],
                       "entry": q["entry"], "exit": price, "qty": q["qty"],
                       "pnl": pnl - fee - q["fee"], "reason": reason})

    def equity_at(i, use_open=False):
        e = cash
        for s, q in pos.items():
            px = O[s][i] if use_open and not np.isnan(O[s][i]) else C[s][i]
            e += q["dir"] * q["qty"] * (px - q["entry"])
        return e

    last_valid = {s: int(np.where(~np.isnan(C[s]) & ~np.isnan(al[s]["close"].to_numpy(float)))[0].max())
                  for s in syms}
    peak = cash
    eq[0] = cash
    for i in range(1, len(idx)):
        # symbol delisted / data ended -> close at its last price
        for s in list(pos):
            if i > last_valid[s]:
                close(s, i, C[s][last_valid[s]], "delisted")
        # exits on signal at open
        for s in syms:
            if np.isnan(O[s][i]):
                continue
            want = T[s][i - 1]
            if blocked[s] and want != blocked[s]:
                blocked[s] = 0
            if s in pos and want != pos[s]["dir"]:
                d = pos[s]["dir"]
                close(s, i, O[s][i] * (1 - d * p.slippage), "signal")
        # entries at open
        for s in syms:
            if np.isnan(O[s][i]) or s in pos:
                continue
            want = T[s][i - 1]
            if want == 0 or want == blocked[s] or np.isnan(A[s][i - 1]) or len(pos) >= max_positions:
                continue
            e = equity_at(i, use_open=True)
            if e <= 0:
                continue
            px = O[s][i] * (1 + want * p.slippage)
            st = initial_stop(px, want, A[s][i - 1], p.atr_mult)
            risk = p.risk_per_trade * R[s][i - 1]
            if risk <= 0:
                continue
            if p.dd_risk_cut > 0 and e < peak * (1 - p.dd_risk_cut):
                risk *= 0.5
            used = sum(q["qty"] * (O[k][i] if not np.isnan(O[k][i]) else C[k][i - 1]) for k, q in pos.items())
            room = max(0.0, e * p.leverage_cap * 0.98 - used)
            q = min(position_size(e, px, st, risk, p.leverage_cap), room / px)
            if q * px < 10:  # below a realistic minimum order
                continue
            f = q * px * p.fee
            cash -= f
            tp = px + want * A[s][i - 1] * p.take_profit_atr if p.take_profit_atr > 0 else None
            pos[s] = {"dir": want, "qty": q, "entry": px, "stop": st, "tp": tp, "time": idx[i], "fee": f,
                      "atr": A[s][i - 1]}
        # stops intrabar, funding, trailing
        for s in list(pos):
            if np.isnan(L[s][i]):
                continue
            q = pos[s]
            if q["dir"] == 1 and L[s][i] <= q["stop"]:
                close(s, i, min(O[s][i], q["stop"]) * (1 - p.slippage), "stop")
                blocked[s] = 1 if p.block_after_stop else 0
                continue
            if q["dir"] == -1 and H[s][i] >= q["stop"]:
                close(s, i, max(O[s][i], q["stop"]) * (1 + p.slippage), "stop")
                blocked[s] = -1 if p.block_after_stop else 0
                continue
            tp = q.get("tp")
            if tp is not None and ((q["dir"] == 1 and H[s][i] >= tp) or (q["dir"] == -1 and L[s][i] <= tp)):
                fill = max(O[s][i], tp) if q["dir"] == 1 else min(O[s][i], tp)
                close(s, i, fill * (1 - q["dir"] * p.slippage), "take_profit")
                blocked[s] = q["dir"]  # wait for the signal to reset before re-entering
                continue
            cash -= q["qty"] * C[s][i] * funding_per_bar
            if p.breakeven_atr > 0 and q["dir"] * (C[s][i] - q["entry"]) >= p.breakeven_atr * q["atr"]:
                q["stop"] = max(q["stop"], q["entry"]) if q["dir"] == 1 else min(q["stop"], q["entry"])
            if p.trailing and not np.isnan(A[s][i]):
                q["stop"] = trail_stop(q["stop"], q["dir"], C[s][i], A[s][i], p.atr_mult)
        eq[i] = equity_at(i)
        peak = max(peak, eq[i])
    for s in list(pos):
        close(s, len(idx) - 1, C[s][-1], "end")
    eq[-1] = cash
    equity = pd.Series(eq, index=idx, name="equity")
    tr = pd.DataFrame(trades)
    res = BacktestResult(equity, tr)
    res.metrics = compute_metrics(equity, tr, p.bar_hours)
    return res
