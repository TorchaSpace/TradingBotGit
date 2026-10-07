"""Websocket order updates wake the bot early; failures fall back to normal polling."""
import asyncio
import threading
import time

from tradingbot.config import Settings
from tradingbot.userstream import UserStream


class FakePro:
    def __init__(self, batches, fail=0):
        self.batches, self.fail, self.closed = list(batches), fail, False

    async def watch_orders(self):
        await asyncio.sleep(0.01)
        if self.fail:
            self.fail -= 1
            raise ConnectionError("ws down")
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(10)
        return []

    async def close(self):
        self.closed = True


S = Settings(mode="demo", api_key="k", api_secret="s").validate()


def test_final_order_updates_reach_the_callback():
    got = []
    fake = FakePro([[{"symbol": "BTC/USDT", "status": "open"}], [{"symbol": "BTC/USDT", "status": "closed", "type": "stop_loss_limit"}]])
    st = UserStream(S, got.append, factory=lambda s: fake).start()
    t0 = time.time()
    while not got and time.time() - t0 < 5:
        time.sleep(0.02)
    st.stop()
    assert [o["status"] for o in got] == ["closed"] and st.events == 1 and fake.closed


def test_stream_gives_up_after_repeated_failures(monkeypatch):
    import tradingbot.userstream as U
    monkeypatch.setattr(U, "MAX_FAILURES", 2)
    st = UserStream(S, lambda o: None, factory=lambda s: FakePro([], fail=10)).start()
    st.thread.join(10)
    assert not st.thread.is_alive() and not st.connected


def test_bad_factory_does_not_raise():
    def boom(s):
        raise RuntimeError("no ccxt.pro")
    st = UserStream(S, lambda o: None, factory=boom).start()
    st.thread.join(5)
    assert not st.thread.is_alive()


def test_bot_sleep_wakes_on_order_event(tmp_path, monkeypatch):
    import tradingbot.live as live
    from test_live_exchange import FakeSpot, UP
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "make_exchange", lambda s, authenticated=None: FakeSpot(UP))
    bot = live.Bot(Settings(mode="paper", symbols=["BTC/USDT"], btc_filter=False).validate())
    threading.Timer(0.3, lambda: bot._on_order_event({"symbol": "BTC/USDT", "status": "closed"})).start()
    t0 = time.time()
    assert bot._sleep(20) is False and time.time() - t0 < 3
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    assert bot._sleep(20, ev) is True
