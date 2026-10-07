"""Every strategy and feature must give the same value at time t whether or not later data exists."""
import numpy as np
import pandas as pd

from tradingbot.lookahead import check, check_all


def _df(n=2600, seed=0):
    idx = pd.date_range("2022-01-01", periods=n, freq="4h", tz="UTC")
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": rng.uniform(1, 5, n)}, index=idx)


def test_no_lookahead_anywhere():
    full = {"ETH/USDT": _df(seed=1), "SOL/USDT": _df(seed=2)}
    for r in check_all(full, _df(seed=3), allow_short=True):
        assert r["ok"], r


def test_detector_catches_future_peeking():
    df = _df()
    assert not check(lambda d: d["close"].shift(-1), df)["ok"]
    assert not check(lambda d: d["close"].rolling(5, center=True).mean(), df)["ok"]
    assert check(lambda d: d["close"].rolling(5).mean(), df)["ok"]
