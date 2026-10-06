"""Portfolio engine, filters and profiles (offline)."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest import BacktestParams, run_backtest, run_portfolio
from tradingbot.config import PROFILES, load_settings
from tradingbot.indicators import adx, higher_tf_trend
from tradingbot.strategies import ema_trend, filters, gate_entries
from test_core import make_df, random_walk


def test_portfolio_single_symbol_matches_single_engine():
    df = random_walk(1200, seed=7)
    tgt = ema_trend(df, False)
    p = BacktestParams(atr_mult=4, trailing=False)
    a = run_backtest(df, tgt, p)
    b = run_portfolio({"X": df}, {"X": tgt}, p, max_positions=1)
    assert b.metrics["trades"] == a.metrics["trades"]
    assert b.equity.iloc[-1] == pytest.approx(a.equity.iloc[-1], rel=1e-6)


def test_portfolio_respects_max_positions_and_cash():
    dfs = {f"S{i}": make_df(100 * np.exp(np.linspace(0, 0.3, 300)), spread=0.002) for i in range(5)}
    tg = {k: pd.Series(1, index=d.index) for k, d in dfs.items()}
    p = BacktestParams(risk_per_trade=0.05, atr_mult=1, leverage_cap=1.0, fee=0, slippage=0)
    r = run_portfolio(dfs, tg, p, max_positions=3)
    assert set(r.trades.symbol) <= {"S0", "S1", "S2"}
    # spot: notional never exceeds equity -> qty*entry summed at start <= start equity
    first = r.trades.groupby("symbol").first()
    assert (first.qty * first.entry).sum() <= 1000 * 1.0001


def test_higher_tf_trend_has_no_lookahead():
    df = random_walk(1000, seed=11)
    full = higher_tf_trend(df, "1D", 20)
    for cut in (500, 733, 900):
        part = higher_tf_trend(df.iloc[:cut], "1D", 20)
        pd.testing.assert_series_equal(full.iloc[:cut], part, check_names=False)


def test_adx_range():
    a = adx(random_walk(600)).dropna()
    assert ((a >= 0) & (a <= 100)).all()


def test_gate_entries_only_blocks_new_entries():
    base = pd.Series([0, 1, 1, 1, 1, 0, 1, 1])
    allow = pd.Series([2, 0, 2, 0, 0, 2, 0, 2])
    out = gate_entries(base, allow).tolist()
    assert out == [0, 0, 1, 1, 1, 0, 0, 1]
    df = random_walk(800, seed=2)
    f = filters(df, adx_min=20, htf=True)
    full = gate_entries(ema_trend(df, True), f)
    part = gate_entries(ema_trend(df.iloc[:600], True), filters(df.iloc[:600], adx_min=20, htf=True))
    pd.testing.assert_series_equal(full.iloc[:600], part, check_names=False)


def test_profiles_load_and_env_overrides(tmp_path, monkeypatch):
    for k in ("PROFILE", "RISK_PER_TRADE", "ATR_STOP_MULT", "MAX_OPEN_POSITIONS", "MODE", "SYMBOLS"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / "missing.env"  # don't let load_dotenv write into os.environ
    monkeypatch.setenv("PROFILE", "conservative")
    s = load_settings(env)
    assert s.risk_per_trade == PROFILES["conservative"]["risk_per_trade"]
    assert s.trailing is False and len(s.symbols) == 8
    monkeypatch.setenv("RISK_PER_TRADE", "0.004")
    assert load_settings(env).risk_per_trade == 0.004


def test_btc_filter_blocks_longs_in_btc_downtrend():
    from tradingbot.strategies import apply_btc_filter
    coin = random_walk(600, seed=4)
    btc_down = make_df(100 * np.exp(np.linspace(0, -0.8, 600)))
    btc_up = make_df(100 * np.exp(np.linspace(0, 0.8, 600)))
    long_all = pd.Series(1, index=coin.index)
    assert (apply_btc_filter(long_all, btc_down) == 0).all()
    assert apply_btc_filter(long_all, btc_up).iloc[300:].eq(1).all()


def test_btc_filter_no_lookahead():
    from tradingbot.strategies import make_target
    coin, btc = random_walk(900, seed=8), random_walk(900, seed=9)
    full = make_target("ema_trend", coin, True, btc)
    part = make_target("ema_trend", coin.iloc[:650], True, btc.iloc[:650])
    pd.testing.assert_series_equal(full.iloc[:650], part, check_names=False)


def test_portfolio_take_profit_and_delisting():
    up = make_df(100 * np.exp(np.linspace(0, 0.4, 300)), spread=0.002)
    gone = up.iloc[:150].copy()          # this coin stops trading half way
    dfs = {"A": up, "B": gone}
    tg = {k: pd.Series(1, index=d.index) for k, d in dfs.items()}
    r = run_portfolio(dfs, tg, BacktestParams(take_profit_atr=1.0, atr_mult=5), max_positions=2)
    assert "take_profit" in set(r.trades.reason)
    b = r.trades[r.trades.symbol == "B"]
    assert b.exit_time.max() <= gone.index[-1] + pd.Timedelta(hours=4)
