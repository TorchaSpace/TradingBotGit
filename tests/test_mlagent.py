"""Paper-only ML agent: training, hourly loop, 24h exits, never sends orders."""
import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
import tradingbot.mlagent as M
from tradingbot.config import Settings


def _hourly(seed, n=2 * 365 * 24):
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.00005, 0.006, n)))
    return pd.DataFrame({"open": c, "high": c * 1.003, "low": c * 0.997, "close": c, "volume": rng.uniform(1, 2, n)},
                        index=idx)


class FakeEx:
    def __init__(self, data, upto):
        self.data, self.upto, self.orders = data, upto, []

    def milliseconds(self):
        return int(self.data["BTC/USDT"].index[self.upto].timestamp() * 1000) + 60_000

    def fetch_ohlcv(self, sym, tf, since=None, limit=1000):
        d = self.data[sym].iloc[: self.upto + 1]
        d = d[d.index >= pd.Timestamp(since, unit="ms", tz="UTC")].head(limit)
        return [[int(t.timestamp() * 1000), *r] for t, r in zip(d.index, d[["open", "high", "low", "close", "volume"]].to_numpy())]

    def fetch_ticker(self, sym):
        return {"last": float(self.data[sym]["close"].iloc[self.upto])}

    def create_order(self, *a, **k):
        self.orders.append(a)
        raise AssertionError("ML agent must never place real orders")


@pytest.fixture
def trained(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "STATE_DIR", tmp_path)
    data = {s: _hourly(i) for i, s in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"])}
    meta = M.train(data, data["BTC/USDT"])
    return data, meta


def test_train_saves_model_and_honest_check(trained):
    data, meta = trained
    assert meta["samples"] > 10000 and meta["holdout_check"]["auc"] > 0.3
    assert M.load_meta()["threshold"] == M.THRESHOLD and not M.needs_retrain()


def test_features_have_no_lookahead():
    data = {s: _hourly(i, 3000) for i, s in enumerate(["BTC/USDT", "ETH/USDT"])}
    full = M.features(data, data["BTC/USDT"])
    cut = {k: v.iloc[:2500] for k, v in data.items()}
    part = M.features(cut, cut["BTC/USDT"])
    t = part.index.max()
    a = full[full.index == t].sort_values("coin")[M.FEATURES].to_numpy()
    b = part[part.index == t].sort_values("coin")[M.FEATURES].to_numpy()
    assert np.allclose(a, b, equal_nan=True)


def test_paper_loop_buys_on_high_probability_and_sells_after_24h(trained, monkeypatch):
    data, _ = trained
    s = Settings(symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"]).validate()
    ex = FakeEx(data, upto=len(data["BTC/USDT"]) - 100)
    agent = M.MLAgent(s, exchange=ex)

    class Always:
        def predict_proba(self, X):
            return np.c_[np.zeros(len(X)), np.ones(len(X))]
    agent.model = Always()
    t0 = data["BTC/USDT"].index[ex.upto]
    agent.step(now=t0)
    assert len(agent.state["positions"]) == 3 and agent.state["cash"] < 1
    assert abs(agent.equity() - M.START_CASH) < 15                 # only fees/slippage lost
    ex.upto += 1
    agent.step(now=t0 + pd.Timedelta(hours=1))
    assert len(agent.state["positions"]) == 3                      # held, not re-bought
    ex.upto += 24
    agent.model = type("Never", (), {"predict_proba": lambda self, X: np.c_[np.ones(len(X)), np.zeros(len(X))]})()
    agent.step(now=t0 + pd.Timedelta(hours=25))
    assert agent.state["positions"] == {} and not ex.orders
    j = pd.read_csv(M._paths()[3])
    assert (j.action == "open").sum() == 3 and (j.action == "close").sum() == 3


def test_agent_refuses_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "STATE_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        M.MLAgent(Settings().validate(), exchange=object())
