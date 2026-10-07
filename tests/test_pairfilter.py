"""Coin safety filter: delisted / thin / brand-new coins get no new entries; delisting exits early."""
import pandas as pd

from tradingbot.config import Settings
from tradingbot.pairfilter import PairFilter

NOW = pd.Timestamp.now(tz="UTC")


class Venue:
    def __init__(self, active=None, volumes=None, listed=None, delist=None):
        self.active, self.volumes, self.listed, self.delist = active or {}, volumes or {}, listed or {}, delist

    def load_markets(self):
        return {s: {"active": a} for s, a in self.active.items()}

    def fetch_tickers(self, syms):
        return {s: {"quoteVolume": v} for s, v in self.volumes.items()}

    def fetch_ohlcv(self, sym, tf, since=None, limit=1):
        t = self.listed.get(sym, NOW - pd.Timedelta(days=2000))
        return [[int(t.timestamp() * 1000), 1, 1, 1, 1, 1]]

    def sapi_get_spot_delist_schedule(self):
        return self.delist or []


S = Settings(symbols=["BTC/USDT", "ETH/USDT", "ZZZ/USDT", "NEW/USDT", "DEAD/USDT"]).validate()


def test_filter_reasons_and_delist_exit():
    trading = Venue(active={"BTC/USDT": True, "ETH/USDT": True, "ZZZ/USDT": True, "NEW/USDT": True, "DEAD/USDT": False})
    public = Venue(volumes={"BTC/USDT": 9e9, "ETH/USDT": 5e9, "ZZZ/USDT": 1e5, "NEW/USDT": 9e7, "DEAD/USDT": 9e7},
                   listed={"NEW/USDT": NOW - pd.Timedelta(days=10)})
    soon = int((NOW + pd.Timedelta(days=3)).timestamp() * 1000)
    auth = Venue(delist=[{"delistTime": soon, "symbols": ["ETHUSDT"]}])
    f = PairFilter(S, trading, public_ex=public, auth_ex=auth)
    b = f.refresh(force=True)
    assert set(b) == {"ETH/USDT", "ZZZ/USDT", "NEW/USDT", "DEAD/USDT"} and "BTC/USDT" not in b
    assert "listeden" in b["ETH/USDT"] and "hacim" in b["ZZZ/USDT"] and "yeni" in b["NEW/USDT"] and "kapalı" in b["DEAD/USDT"]
    assert f.must_exit("ETH/USDT") and f.must_exit("BTC/USDT") is None


def test_filter_never_blocks_when_data_is_missing():
    class Broken:
        def load_markets(self):
            raise ConnectionError

        def fetch_tickers(self, s):
            raise ConnectionError

        def fetch_ohlcv(self, *a, **k):
            raise ConnectionError
    f = PairFilter(S, Broken(), public_ex=Broken())
    assert f.refresh(force=True) == {}


def test_bot_skips_entry_in_blocked_coin(tmp_path, monkeypatch):
    import tradingbot.live as live
    from test_live_exchange import FakeSpot, UP
    fx = FakeSpot(UP)
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(live, "make_exchange", lambda s, authenticated=None: fx)
    bot = live.Bot(Settings(mode="paper", symbols=["BTC/USDT"], btc_filter=False).validate())
    bot.pairs.refresh = lambda force=False: bot.pairs.blocked
    bot.pairs.blocked = {"BTC/USDT": "24s hacim düşük"}
    bot.run(once=True)
    assert "BTC/USDT" not in bot.trades
    bot.pairs.blocked = {}
    bot.state["last_bar"] = {}
    bot.run(once=True)
    assert "BTC/USDT" in bot.trades
