"""Carry engine logic, maker-first execution and notifier (offline)."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.carry import CarryParams, carry_backtest, carry_signal, select
from tradingbot.data import funding_8h
from tradingbot.execution import execute
from tradingbot.notify import Notifier


def test_notifier_is_noop_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = Notifier()
    assert not n.enabled and n.send("x") is False


def test_funding_8h_sums_4h_payments():
    idx = pd.to_datetime(["2024-01-01 04:00:00.005", "2024-01-01 08:00:00.003", "2024-01-01 16:00:00.000"],
                         utc=True, format="mixed")
    f = funding_8h(pd.Series([0.0001, 0.0002, 0.0005], index=idx))
    assert f.loc[pd.Timestamp("2024-01-01 08:00", tz="UTC")] == pytest.approx(0.0003)
    assert f.loc[pd.Timestamp("2024-01-01 16:00", tz="UTC")] == pytest.approx(0.0005)


def test_carry_signal_and_selection():
    s = pd.Series([0.0001] * 8 + [0.0004])
    assert carry_signal(s, 9) == pytest.approx((0.0008 + 0.0004) / 9)
    assert np.isnan(carry_signal(s.iloc[:3], 9))
    held, opened, closed = select({"A": 0.0003, "B": 0.0002, "C": 0.00005, "D": -0.0001},
                                  {"C", "D"}, slots=2, enter=0.0001, exit_=0.0)
    assert closed == ["D"] and held == {"C", "A"} and opened == ["A"]


def _carry_data(f_rate, n=400):
    idx = pd.date_range("2022-01-01", periods=n, freq="8h", tz="UTC")
    px = pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.02, n))), index=idx)
    return {"X": pd.Series(f_rate, index=idx)}, {"X": px}, {"X": px * 1.0001}


def test_carry_backtest_earns_funding_and_pays_costs():
    F, S, P = _carry_data(0.0003)
    e, log_df, m = carry_backtest(F, S, P, CarryParams(slots=1, leverage=2))
    expected = (1 + 0.0003 * 2 / 3) ** (400 - 10) * (1 - 0.004 / 2 * 2 / 3)
    assert e.iloc[-1] / 1000 == pytest.approx(expected, rel=0.02)   # price risk hedged away
    F, S, P = _carry_data(-0.0003)
    e, log_df, m = carry_backtest(F, S, P, CarryParams(slots=1))
    assert len(log_df) == 0 and e.iloc[-1] == pytest.approx(1000)  # never enters on negative funding


class FakeEx:
    def __init__(self, maker_fill):
        self.maker_fill, self.orders = maker_fill, []

    def fetch_order_book(self, s, n):
        return {"bids": [[99.0, 1]], "asks": [[101.0, 1]]}

    def create_order(self, sym, typ, side, qty, price, params):
        self.orders.append((typ, qty, price, params))
        if typ == "limit":
            return {"id": "1", "status": "open", "filled": 0}
        return {"id": "2", "status": "closed", "filled": qty, "average": 100.5}

    def fetch_order(self, oid, sym):
        return {"id": oid, "status": "open", "filled": self.maker_fill, "average": 99.0}

    def cancel_order(self, oid, sym):
        pass

    def amount_to_precision(self, sym, q):
        return f"{q:.4f}"

    def fetch_ticker(self, sym):
        return {"last": 100.0}


def test_maker_first_partial_fill_then_market():
    ex = FakeEx(maker_fill=0.6)
    filled, avg = execute(ex, "X/USDT", "buy", 1.0, maker_first=True, wait=0, sleep=lambda s: None)
    assert filled == pytest.approx(1.0)
    assert avg == pytest.approx((0.6 * 99 + 0.4 * 100.5) / 1.0)
    assert ex.orders[0][3]["postOnly"] is True and ex.orders[1][0] == "market"


def test_market_only_by_default():
    ex = FakeEx(maker_fill=0)
    filled, avg = execute(ex, "X/USDT", "buy", 1.0)
    assert [o[0] for o in ex.orders] == ["market"] and filled == 1.0
