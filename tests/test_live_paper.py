"""Bot loop in paper mode against a fake exchange (no network)."""
import numpy as np
import pytest

import tradingbot.live as live
from tradingbot.config import Settings


class FakeExchange:
    def __init__(self, closes, tf_ms=4 * 3600 * 1000):
        self.tf_ms = tf_ms
        self.t0 = 1_700_000_000_000
        self.closes = list(closes)
        self.n = len(self.closes)

    def parse_timeframe(self, tf):
        return self.tf_ms // 1000

    def milliseconds(self):
        # "now" is just after the last candle closed
        return self.t0 + self.n * self.tf_ms + 1

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        rows = []
        start = max(0, self.n - limit)
        for i in range(start, self.n):
            c = self.closes[i]
            o = self.closes[i - 1] if i else c
            rows.append([self.t0 + i * self.tf_ms, o, max(o, c) * 1.005, min(o, c) * 0.995, c, 1.0])
        return rows

    def fetch_ticker(self, symbol):
        return {"last": self.closes[self.n - 1]}

    def step(self, price):
        self.closes.append(price)
        self.n += 1


@pytest.fixture
def bot_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "STOP_FILE", tmp_path / "STOP")

    def make(closes, **kw):
        fx = FakeExchange(closes)
        monkeypatch.setattr(live, "make_exchange", lambda s, authenticated=None: fx)
        s = Settings(mode="paper", market=kw.pop("market", "spot"), symbols=["BTC/USDT"],
                     strategy=kw.pop("strategy", "ema_trend"), **kw).validate()
        return live.Bot(s), fx
    return make


def test_paper_bot_enters_uptrend_and_trails(bot_factory):
    closes = 100 * np.exp(np.linspace(0, 0.5, 400))
    bot, fx = bot_factory(closes, risk_per_trade=0.01, atr_stop_mult=2.0, trailing=True)
    bot.run(once=True)
    tr = bot.state["trades"].get("BTC/USDT")
    assert tr and tr["direction"] == 1
    # risk ~1% of 1000 -> qty * stop distance ~= 10
    assert abs(tr["qty"] * (tr["entry"] - tr["stop"]) - 10) < 1.5
    first_stop = tr["stop"]
    for k in range(5):
        fx.step(fx.closes[-1] * 1.01)
        bot.run(once=True)
    assert bot.state["trades"]["BTC/USDT"]["stop"] > first_stop


def test_paper_trailing_off_by_default(bot_factory):
    closes = 100 * np.exp(np.linspace(0, 0.5, 400))
    bot, fx = bot_factory(closes)
    bot.run(once=True)
    first = bot.state["trades"]["BTC/USDT"]["stop"]
    for _ in range(5):
        fx.step(fx.closes[-1] * 1.01)
        bot.run(once=True)
    assert bot.state["trades"]["BTC/USDT"]["stop"] == first


def test_paper_stop_out_blocks_reentry(bot_factory):
    closes = 100 * np.exp(np.linspace(0, 0.5, 400))
    bot, fx = bot_factory(closes)
    bot.run(once=True)
    assert "BTC/USDT" in bot.state["trades"]
    stop = bot.state["trades"]["BTC/USDT"]["stop"]
    fx.step(stop * 0.995)                  # dip just through the stop; trend signal still long
    bot.run(once=True)
    assert "BTC/USDT" not in bot.state["trades"]   # stopped and NOT immediately re-bought
    assert bot.state["blocked"].get("BTC/USDT") == 1
    assert bot.broker.cash < 1000


def test_kill_switch_flattens_and_halts(bot_factory):
    closes = 100 * np.exp(np.linspace(0, 0.5, 400))
    bot, fx = bot_factory(closes, max_drawdown=0.05)
    bot.run(once=True)
    bot.broker.cash -= 80                   # simulate a large loss elsewhere
    bot.run(once=True)
    assert bot.risk.state.halted
    assert bot.state["trades"] == {}
    assert bot.broker.positions == {}


def test_stop_file_exits_immediately(bot_factory, tmp_path):
    bot, fx = bot_factory(100 * np.ones(400))
    (tmp_path / "STOP").write_text("")
    bot.run()  # would loop forever without the STOP file
