"""Learning trade filter: model maths, approval gate, live safety."""
import numpy as np
import pandas as pd
import pytest

import tradingbot.learner as L
from tradingbot.config import ConfigError, Settings


def test_logistic_learns_and_auc():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, 3))
    y = (X[:, 0] - 0.5 * X[:, 1] + rng.normal(0, 0.5, 2000) > 0).astype(float)
    m = L.fit_logistic(X, y)
    p = L.predict(m, X)
    assert L.auc(y, p) > 0.9
    assert L.auc(y, rng.random(2000)) == pytest.approx(0.5, abs=0.05)


def test_approval_gate():
    good = [{"year": y, "auc": 0.62, "skipped_pnl": -50.0} for y in range(2022, 2027)]
    assert L.approve(good)[0] is True
    weak = [dict(r, auc=0.51) for r in good]
    assert L.approve(weak)[0] is False
    hurt_recently = good[:3] + [dict(good[3], skipped_pnl=10.0), good[4]]
    assert L.approve(hurt_recently)[0] is False
    assert L.approve([])[0] is False


def test_learner_mode_setting():
    assert Settings().validate().learner_mode == "shadow"
    with pytest.raises(ConfigError):
        Settings(learner_mode="yolo").validate()


def _df(n=600, seed=1):
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    c = 100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0.001, 0.01, n)))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0}, index=idx)


def test_live_filter_is_inactive_unless_approved_and_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "STATE_DIR", tmp_path)
    s = Settings(learner_mode="filter").validate()
    assert L.LiveFilter(s).check(_df(), None, 1) == (True, None)          # no model at all
    m = {"model": {"mu": [0] * 8, "sd": [1] * 8, "w": [-10] + [0] * 8}, "threshold": 0.5, "approved": False}
    (tmp_path / "learner_spot.json").write_text(__import__("json").dumps(m))
    assert L.LiveFilter(s).active is False                                  # not approved -> never filters
    m["approved"] = True
    (tmp_path / "learner_spot.json").write_text(__import__("json").dumps(m))
    assert L.LiveFilter(Settings(learner_mode="shadow").validate()).active is False
    f = L.LiveFilter(s)
    allow, p = f.check(_df(), None, 1)
    assert f.active and allow is False and p < 0.01                         # approved + filter -> skips weak signal
    allow, p = f.check(pd.DataFrame(), None, 1)                             # broken input -> allow, never raise
    assert allow is True


def test_entry_features_have_no_lookahead():
    df = _df()
    f = L.feature_frame(df, None)
    t = df.index[400]
    x = L.entry_features(f, t, 1)
    expected = f.loc[df.index[399], L.FEATURES].to_numpy(float)
    assert np.allclose(x, expected)
    xs = L.entry_features(f, t, -1)
    k = L.FEATURES.index("trend_gap")
    assert xs[k] == pytest.approx(-x[k]) and xs[L.FEATURES.index("atr_pct")] == pytest.approx(x[L.FEATURES.index("atr_pct")])


def test_build_end_to_end(tmp_path, monkeypatch):
    from test_validation import _candles
    monkeypatch.setattr(L, "STATE_DIR", tmp_path)
    s = Settings(symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"]).validate()
    full = {sym: _candles(i, 0.0012 + 0.0003 * i) for i, sym in enumerate(s.symbols)}
    r = L.build(s, full, full["BTC/USDT"])
    assert r["trades"] > 100 and r["walk_forward"] and isinstance(r["approved"], bool)
    assert L.load_model("spot")["trades"] == r["trades"] and not L.needs_retrain("spot")
    j = pd.DataFrame([{"time": str(full["BTC/USDT"].index[-50]), "action": "open", "symbol": "BTC/USDT", "direction": 1,
                       "qty": 1, "price": 1, "stop": 1, "reason": "signal", "pnl_est": 0, "entry": 1, "equity": 1}])
    sh = L.shadow_report(s, full, full["BTC/USDT"], j)
    assert len(sh) == 1 and 0 <= sh[0]["prob"] <= 1 and sh[0]["pnl"] is None
