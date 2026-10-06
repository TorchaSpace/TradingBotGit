"""Offline tests: no network, no API keys."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest import BacktestParams, run_backtest
from tradingbot.config import ConfigError, Settings
from tradingbot.indicators import atr, ema, rsi
from tradingbot.risk import RiskManager, position_size, trail_stop
from tradingbot.strategies import STRATEGIES


def make_df(closes, spread=0.01):
    closes = np.asarray(closes, float)
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="4h", tz="UTC")
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "open": opens,
        "high": np.maximum(opens, closes) * (1 + spread),
        "low": np.minimum(opens, closes) * (1 - spread),
        "close": closes,
        "volume": 1.0,
    }, index=idx)


def random_walk(n=1500, seed=0):
    rng = np.random.default_rng(seed)
    return make_df(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


# ---------------------------------------------------------------- config
def test_live_requires_confirmation():
    with pytest.raises(ConfigError):
        Settings(mode="live", api_key="k", api_secret="s").validate()
    Settings(mode="live", api_key="k", api_secret="s", live_confirm="I_UNDERSTAND_THE_RISK").validate()


def test_demo_requires_keys_and_paper_does_not():
    with pytest.raises(ConfigError):
        Settings(mode="demo").validate()
    Settings(mode="paper").validate()


def test_spot_never_leverages_or_shorts():
    s = Settings(market="spot", max_leverage=5)
    assert s.leverage_cap == 1.0 and not s.allow_short


def test_symbols_normalised_per_market():
    assert Settings(market="futures", symbols=["BTC/USDT"]).validate().symbols == ["BTC/USDT:USDT"]
    assert Settings(market="spot", symbols=["BTC/USDT:USDT"]).validate().symbols == ["BTC/USDT"]


def test_risk_limits_are_bounded():
    with pytest.raises(ConfigError):
        Settings(risk_per_trade=0.2).validate()
    with pytest.raises(ConfigError):
        Settings(market="futures", max_leverage=50).validate()


# ---------------------------------------------------------------- indicators
def test_rsi_bounds_and_trend():
    up = make_df(np.linspace(100, 200, 100))
    r = rsi(up["close"]).dropna()
    assert (r >= 0).all() and (r <= 100).all()
    assert r.iloc[-1] > 90


def test_ema_and_atr_shapes():
    df = random_walk(300)
    assert ema(df["close"], 20).isna().sum() == 19
    assert (atr(df).dropna() > 0).all()


# ---------------------------------------------------------------- no look-ahead
@pytest.mark.parametrize("name", list(STRATEGIES))
@pytest.mark.parametrize("short", [False, True])
def test_strategies_have_no_lookahead(name, short):
    """Signal at bar t must not change when future bars are appended/removed."""
    df = random_walk(800, seed=3)
    fn = STRATEGIES[name].fn
    full = fn(df, short)
    for cut in (400, 550, 700):
        partial = fn(df.iloc[:cut], short)
        pd.testing.assert_series_equal(full.iloc[:cut], partial, check_names=False)


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_spot_strategies_never_short(name):
    assert (STRATEGIES[name].fn(random_walk(), False) >= 0).all()


# ---------------------------------------------------------------- risk
def test_position_size_risk_and_cap():
    # 1% of 1000 = 10 USDT risk, stop 5 away -> 2 units, notional 200 (< cap 1000)
    assert position_size(1000, 100, 95, 0.01, 1.0) == pytest.approx(2.0)
    # very tight stop would want 100 units = 10000 notional -> capped at 1x equity = 10 units
    assert position_size(1000, 100, 99.9, 0.01, 1.0) == pytest.approx(10.0)
    assert position_size(1000, 100, 100, 0.01, 1.0) == 0


def test_trailing_stop_only_moves_favourably():
    assert trail_stop(90, 1, 100, 2, 2) == 96
    assert trail_stop(96, 1, 95, 2, 2) == 96
    assert trail_stop(110, -1, 100, 2, 2) == 104
    assert trail_stop(104, -1, 106, 2, 2) == 104


def test_risk_manager_kill_switch(tmp_path):
    rm = RiskManager(0.03, 0.15, 2, tmp_path / "r.json")
    rm.update(1000)
    assert rm.can_open(1000, 0)[0]
    assert not rm.can_open(1000, 2)[0]          # max positions
    assert not rm.can_open(965, 0)[0]           # daily loss 3.5%
    rm.update(840)                               # 16% drawdown
    assert rm.state.halted
    # survives restart
    assert RiskManager(0.03, 0.15, 2, tmp_path / "r.json").state.halted


# ---------------------------------------------------------------- engine
def test_engine_charges_fees_on_flat_market():
    df = make_df(np.full(400, 100.0), spread=0.0)
    df["high"] += 1
    df["low"] -= 1
    tgt = pd.Series(0, index=df.index)
    tgt.iloc[50:60] = 1
    tgt.iloc[100:110] = 1
    res = run_backtest(df, tgt, BacktestParams(fee=0.001, slippage=0.0))
    assert res.metrics["trades"] == 2
    assert res.equity.iloc[-1] < 1000  # only fees paid


def test_engine_stop_loss_limits_loss():
    closes = np.r_[np.full(60, 100.0), np.linspace(100, 50, 40)]
    df = make_df(closes, spread=0.002)
    tgt = pd.Series(1, index=df.index)
    p = BacktestParams(risk_per_trade=0.01, fee=0.0, slippage=0.0, trailing=False)
    res = run_backtest(df, tgt, p)
    first = res.trades.iloc[0]
    assert first.reason == "stop"
    # loss on a stopped trade should be about 1% of equity (gaps can make it a bit worse)
    assert -15 < first.pnl < -8


def test_engine_enters_next_bar_open():
    df = random_walk(300, seed=5)
    tgt = pd.Series(0, index=df.index)
    tgt.iloc[100] = 1
    res = run_backtest(df, tgt, BacktestParams(slippage=0.0))
    t = res.trades.iloc[0]
    assert t.entry_time == df.index[101]
    assert t.entry == pytest.approx(df["open"].iloc[101])


def test_futures_shorts_profit_in_downtrend():
    df = make_df(100 * np.exp(np.linspace(0, -0.7, 600)), spread=0.003)
    tgt = pd.Series(-1, index=df.index)
    res = run_backtest(df, tgt, BacktestParams(allow_short=True, leverage_cap=3, fee=0.0005))
    assert res.metrics["total_return_%"] > 0
    spot = run_backtest(df, tgt, BacktestParams(allow_short=False))
    assert spot.metrics["trades"] == 0
