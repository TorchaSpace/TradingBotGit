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


class FakeDemo(FakeEx):
    """Demo-like spot exchange: market orders fill at the last close; balances are tracked."""

    def __init__(self, data, upto):
        super().__init__(data, upto)
        self.bal = {"USDT": 5000.0}

    def amount_to_precision(self, sym, q):
        return f"{q:.6f}"

    def fetch_balance(self):
        return {"free": dict(self.bal), "total": dict(self.bal)}

    def create_order(self, sym, typ, side, qty, price=None, params=None):
        assert typ == "market"
        px = self.fetch_ticker(sym)["last"]
        base = sym.split("/")[0]
        if side == "buy":
            self.bal["USDT"] -= qty * px
            self.bal[base] = self.bal.get(base, 0) + qty
        else:
            assert self.bal.get(base, 0) >= qty - 1e-9, "must only sell coins it owns"
            self.bal[base] -= qty
            self.bal["USDT"] += qty * px
        self.orders.append((sym, side, qty))
        return {"id": "1", "status": "closed", "filled": qty, "average": px}


def test_demo_account_trades_within_budget_and_only_demo(trained):
    data, _ = trained
    with pytest.raises(RuntimeError):                              # never the real account
        M.MLAgent(Settings(mode="live", api_key="k", api_secret="s", live_confirm="I_UNDERSTAND_THE_RISK").validate(),
                  exchange=object(), account="demo")
    with pytest.raises(ValueError):
        M._paths("live")
    s = Settings(mode="demo", market="spot", api_key="k", api_secret="s",
                 symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"]).validate()
    ex = FakeDemo(data, upto=len(data["BTC/USDT"]) - 100)
    agent = M.MLAgent(s, exchange=ex, account="demo", budget=600)
    agent.model = type("A", (), {"predict_proba": lambda self, X: np.c_[np.zeros(len(X)), np.ones(len(X))]})()
    t0 = data["BTC/USDT"].index[ex.upto]
    agent.step(now=t0)
    assert len(agent.state["positions"]) == 3 and 5000 - ex.bal["USDT"] <= 600 + 1   # budget respected
    ex.upto += 30
    agent.model = type("N", (), {"predict_proba": lambda self, X: np.c_[np.ones(len(X)), np.zeros(len(X))]})()
    agent.step(now=t0 + pd.Timedelta(hours=30))
    assert agent.state["positions"] == {} and [o[1] for o in ex.orders].count("sell") == 3
    lc = M.learning_curve(pd.read_csv(M._paths("demo")[3]), 0.56)
    assert lc["total"] == 3 and lc["weeks"] and lc["expected_win"] == 0.56


def test_agent_switches_to_retrained_model(trained):
    import os
    import pickle
    data, _ = trained
    s = Settings(symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"]).validate()
    ex = FakeEx(data, upto=len(data["BTC/USDT"]) - 100)
    agent = M.MLAgent(s, exchange=ex)
    old = agent.model
    mp = M._paths()[0]
    with open(mp, "wb") as fh:
        pickle.dump({"marker": 1}, fh)
    os.utime(mp, (agent._model_mtime + 10, agent._model_mtime + 10))
    assert agent._reload_model() is True and agent.model == {"marker": 1} and agent.model is not old
    assert agent._reload_model() is False


def test_per_trade_learning_moves_confidence_and_stays_bounded():
    c = {"a": 1.0, "b": 0.0, "n": 0, "wins": 0}
    for _ in range(30):                                   # its 70% signals keep losing -> less confident
        c = M.learn_from_trade(c, 0.70, False)
    assert c["n"] == 30 and c["wins"] == 0 and M.calibrate(0.70, c) < 0.65
    assert 0.5 <= c["a"] <= 2.0 and -1.0 <= c["b"] <= 1.0
    w = {"a": 1.0, "b": 0.0, "n": 0, "wins": 0}
    for _ in range(30):                                   # its 60% signals keep winning -> more confident
        w = M.learn_from_trade(w, 0.60, True)
    assert M.calibrate(0.60, w) > 0.60
    assert M.calibrate(0.66, {"a": 1.0, "b": 0.0}) == pytest.approx(0.66)


def test_universe_and_daily_retrain(tmp_path, monkeypatch):
    s = Settings(symbols=["BTC/USDT", "PEPE/USDT"]).validate()
    u = M.universe(s)
    assert len(u) == len(M.TRAIN_BASES) + 1 and u[0] == "BTC/USDT" and "PEPE/USDT" in u
    monkeypatch.setattr(M, "STATE_DIR", tmp_path)
    (tmp_path / "mlagent_meta.json").write_text('{"trained_at": "2020-01-01T00:00:00+00:00"}')
    assert M.needs_retrain()


def test_hourly_archive_download(tmp_path, monkeypatch):
    import io
    import urllib.request
    import zipfile
    import tradingbot.data as D
    monkeypatch.setattr(D, "CACHE_DIR", tmp_path)

    def fake_urlopen(url, timeout=60):
        name = url.rsplit("/", 1)[1].replace(".zip", ".csv")
        y, m = int(name[-11:-7]), int(name[-6:-4])
        if (y, m) < (2026, 1):
            raise urllib.error.HTTPError(url, 404, "nf", None, None)
        t0 = pd.Timestamp(f"{y}-{m:02d}-01", tz="UTC")
        mult = 1000 if y >= 2025 else 1                          # microsecond archives since 2025
        rows = [f"{int((t0 + pd.Timedelta(hours=h)).timestamp() * 1000 * mult)},1,2,0.5,1.5,10,0,0,0,0,0,0"
                for h in range(24)]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, "\n".join(rows))
        buf.seek(0)
        return buf
    import urllib.error  # noqa: F401
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    n = D.load_hourly_archive("XYZ/USDT", "2025-06-01")
    d = pd.read_csv(tmp_path / "spot_XYZUSDT_1h.csv", index_col=0, parse_dates=True)
    assert n == len(d) > 24 and d.index[0] == pd.Timestamp("2026-01-01", tz="UTC") and d.index.is_monotonic_increasing
    assert D.load_hourly_archive("XYZ/USDT", "2025-06-01") == 0      # cached -> nothing to do
