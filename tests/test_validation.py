"""Statistics behind `python -m tradingbot validate` (PSR, DSR, PBO, bootstrap) and an end-to-end smoke run."""
import json
import math

import numpy as np
import pandas as pd
import pytest

import tradingbot.validation as V
from tradingbot.config import Settings


def test_norm_ppf_inverts_cdf():
    for p in (0.001, 0.025, 0.3, 0.5, 0.8, 0.975, 0.999):
        assert V._norm_cdf(V._norm_ppf(p)) == pytest.approx(p, abs=1e-7)


def test_psr_and_dsr():
    rng = np.random.default_rng(0)
    good = pd.Series(rng.normal(0.002, 0.01, 1500))       # daily Sharpe 0.2 -> ~3.8 annual
    noise = pd.Series(rng.normal(0.0, 0.01, 1500))
    assert V.probabilistic_sharpe(good) > 0.99
    assert 0.02 < V.probabilistic_sharpe(noise) < 0.98
    # deflating for many trials can only lower the probability
    trials = list(rng.normal(0, 0.03, 50))
    d_few = V.deflated_sharpe(good, trials, 5)["dsr"]
    d_many = V.deflated_sharpe(good, trials, 1000)["dsr"]
    assert d_many <= d_few <= V.probabilistic_sharpe(good)
    # a minimum trial dispersion raises the luck threshold
    assert V.deflated_sharpe(noise, [0.0, 0.0001], 100, min_std_per_period=0.05)["sr0_per_period"] > 0.05


def test_pbo_noise_vs_real_edge():
    rng = np.random.default_rng(1)
    noise = pd.DataFrame(rng.normal(0, 0.01, (1600, 10)))
    assert 0.3 < V.pbo_cscv(noise, blocks=8)["pbo"] < 0.7     # picking the in-sample best is a coin flip
    edge = noise.copy()
    edge[0] += 0.004                                          # one config is truly better everywhere
    assert V.pbo_cscv(edge, blocks=8)["pbo"] < 0.05


def test_block_bootstrap_constant_returns():
    daily = pd.Series([0.001] * 400)
    mc = V.block_bootstrap(daily, horizon=365, sims=200)
    assert mc["median"] == pytest.approx(1.001 ** 365 - 1, rel=1e-9)
    assert mc["prob_loss"] == 0 and mc["median_max_dd"] == 0
    assert len(mc["fan"][50]) == 53 and sum(mc["hist"]) == 200


def test_losing_streaks():
    tr = pd.DataFrame({"pnl": [1, -1, -1, -1, 2, -1, -1, 3]})
    st = V.losing_streaks(tr, sims=500)
    assert st["historical_worst_streak"] == 3 and st["win_rate"] == pytest.approx(3 / 8)
    assert st["p95_worst_streak_per_100"] >= st["median_worst_streak_per_100"] > 3


def _candles(seed, drift, start="2020-01-01", end="2026-06-01"):
    idx = pd.date_range(start, end, freq="4h", tz="UTC")
    rng = np.random.default_rng(seed)
    # alternating trending regimes so the trend strategy actually trades
    regime = np.sign(np.sin(np.arange(len(idx)) / 700)) * drift
    close = 100 * np.exp(np.cumsum(regime + rng.normal(0, 0.012, len(idx))))
    high = close * (1 + np.abs(rng.normal(0, 0.006, len(idx))))
    low = close * (1 - np.abs(rng.normal(0, 0.006, len(idx))))
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                         "volume": 1000.0}, index=idx)


def test_run_validation_end_to_end(tmp_path, monkeypatch):
    s = Settings(symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"]).validate()
    full = {sym: _candles(i, 0.0012 + 0.0003 * i) for i, sym in enumerate(s.symbols)}
    msgs = []
    res = V.run_validation(s, full, full["BTC/USDT"], progress=msgs.append)
    assert msgs[0] == "ana backtest" and len(res["grid"]) == 16
    assert set(res["periods"]) == {"all", "train", "holdout"}
    assert 0 <= res["pbo"]["pbo"] <= 1 and 0 <= res["dsr"]["dsr"] <= res["dsr"]["dsr_effective"] <= 1
    assert [c["risk"] for c in res["risk_curve"]] == list(V.RISK_LEVELS)
    assert all(v["state"] in ("ok", "warn", "info") for v in res["verdict"])

    # live tracking: a paper equity curve that fell 30% in 30 days must be flagged
    state = tmp_path / "state"
    state.mkdir()
    t = pd.date_range("2026-09-01", periods=30 * 24, freq="h", tz="UTC")
    pd.DataFrame({"time": t.astype(str), "equity": np.linspace(1000, 700, len(t))}).to_csv(
        state / "equity_paper_spot.csv", index=False)
    monkeypatch.setattr(V, "OUT_DIR", tmp_path / "reports")
    j, h = V.save(res, state)
    live = res["live"][0]
    assert live["mode"] == "paper" and live["state"] == "warn" and live["return"] == pytest.approx(-0.3)
    page = h.read_text(encoding="utf-8")
    assert "Strateji doğrulama raporu" in page and "Gerçek takip" in page and "PAPER" in page
    loaded = json.loads(j.read_text())
    assert loaded["verdict"] == res["verdict"]
    # the band is read back from JSON (string keys) the same way
    assert V._fan_at(loaded["mc_all"]["fan"], 100, 50) == pytest.approx(V._fan_at(res["mc_all"]["fan"], 100, 50))
    assert not math.isnan(res["psr"])
