"""ExchangeBroker / Bot safety behaviour against a fake exchange (no network, no keys)."""
import ccxt
import numpy as np
import pytest

import tradingbot.live as live
import tradingbot.carry as carry
from tradingbot.config import Settings

TF = 4 * 3600 * 1000


class FakeSpot:
    """Minimal spot exchange: market orders fill at last price, stop orders can be rejected."""

    def __init__(self, closes, reject_stops=False, extra_coins=0.0):
        self.closes = list(closes)
        self.t0 = 1_700_000_000_000
        self.bal = {"USDT": 1000.0, "BTC": extra_coins}   # extra_coins = user's own BTC
        self.locked = 0.0
        self.reject_stops = reject_stops
        self.orders = []
        self.open_stops = []

    # market data
    def parse_timeframe(self, tf): return TF // 1000
    def milliseconds(self): return self.t0 + len(self.closes) * TF + 1
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        n = len(self.closes)
        return [[self.t0 + i * TF, self.closes[i - 1] if i else self.closes[0],
                 self.closes[i] * 1.005, self.closes[i] * 0.995, self.closes[i], 1.0]
                for i in range(max(0, n - limit), n)]
    def fetch_ticker(self, s): return {"last": self.closes[-1]}
    def load_markets(self): return {}
    def market(self, s): return {"base": "BTC", "limits": {"cost": {"min": 5}}}
    def amount_to_precision(self, s, q): return f"{q:.6f}"
    def price_to_precision(self, s, p): return f"{p:.2f}"

    # account
    def fetch_balance(self):
        btc = self.bal["BTC"]
        return {"total": dict(self.bal), "free": {"USDT": self.bal["USDT"], "BTC": btc - self.locked}}

    # orders
    def create_order(self, s, typ, side, qty, price=None, params=None):
        params = params or {}
        self.orders.append((typ, side, qty, params))
        if "stopLossPrice" in params:
            if self.reject_stops:
                raise ccxt.InvalidOrder("binance -2010 stop rejected")
            self.open_stops.append(qty)
            self.locked += qty
            return {"id": "s", "status": "open"}
        px = self.closes[-1]
        if side == "buy":
            self.bal["USDT"] -= qty * px
            self.bal["BTC"] += qty
        else:
            self.bal["USDT"] += qty * px
            self.bal["BTC"] -= qty
        return {"id": "m", "status": "closed", "filled": qty, "average": px}

    def cancel_all_orders(self, s, params=None):
        self.open_stops, self.locked = [], 0.0

    def step(self, px): self.closes.append(px)


@pytest.fixture
def make_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "STOP_FILE", tmp_path / "STOP")

    def make(fx, **kw):
        monkeypatch.setattr(live, "make_exchange", lambda s, authenticated=None: fx)
        s = Settings(mode="demo", api_key="k", api_secret="s", market="spot", symbols=["BTC/USDT"],
                     btc_filter=False, **kw).validate()
        return live.Bot(s)
    return make


UP = 100 * np.exp(np.linspace(0, 0.5, 400))


def test_open_is_tracked_and_protected(make_bot):
    fx = FakeSpot(UP)
    bot = make_bot(fx)
    bot.run(once=True)
    tr = bot.trades["BTC/USDT"]
    assert tr["stop_ok"] is True and fx.open_stops, "stop order must be placed"
    rows = (live.STATE_DIR / "journal_demo_spot.csv").read_text().splitlines()
    assert rows[1].split(",")[1] == "open"


def test_rejected_stop_keeps_trade_tracked_and_backup_stop_closes(make_bot):
    fx = FakeSpot(UP, reject_stops=True)
    bot = make_bot(fx)
    bot.run(once=True)
    tr = bot.trades["BTC/USDT"]
    assert tr["stop_ok"] is False            # still tracked although the stop was rejected
    fx.step(tr["stop"] * 0.98)                # price gaps through the stop
    bot.run(once=True)
    assert "BTC/USDT" not in bot.trades
    assert fx.bal["BTC"] == pytest.approx(0, abs=1e-6)   # sold at market by the bot itself
    journal = (live.STATE_DIR / "journal_demo_spot.csv").read_text()
    assert "backup_stop" in journal


def test_users_own_coins_are_never_touched_or_counted(make_bot):
    fx = FakeSpot(UP, extra_coins=0.5)        # user already holds 0.5 BTC
    bot = make_bot(fx)
    eq0 = bot.broker.equity()
    assert eq0 == pytest.approx(1000.0)       # the user's BTC is not part of the bot's equity
    bot.run(once=True)
    bought = bot.trades["BTC/USDT"]["qty"]
    fx.step(fx.closes[-1] * 0.5)              # crash -> exit everything the bot owns
    bot.run(once=True)
    assert fx.bal["BTC"] == pytest.approx(0.5, rel=1e-6)   # user's 0.5 BTC untouched
    assert bought > 0


def test_failing_symbol_is_retried_not_skipped(make_bot, monkeypatch):
    fx = FakeSpot(UP)
    bot = make_bot(fx)
    calls = {"n": 0}
    orig = bot.on_bar

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ccxt.NetworkError("timeout")
        return orig(*a, **k)
    monkeypatch.setattr(bot, "on_bar", flaky)
    bot.run(once=True)
    assert "BTC/USDT" not in bot.trades and not bot.state["last_bar"]
    bot.run(once=True)                         # same bar retried on the next loop
    assert "BTC/USDT" in bot.trades


class FakeFut(FakeSpot):
    def __init__(self, closes, foreign_short=0.0):
        super().__init__(closes)
        self.pos = -foreign_short              # e.g. a carry hedge in the same account
    def set_position_mode(self, h): pass
    def set_margin_mode(self, *a): pass
    def set_leverage(self, *a): pass
    def fetch_positions(self, syms):
        if self.pos == 0:
            return []
        return [{"symbol": "BTC/USDT:USDT", "contracts": abs(self.pos),
                 "side": "long" if self.pos > 0 else "short", "entryPrice": 100.0}]
    def fetch_balance(self):
        return {"info": {"totalMarginBalance": 1000.0}, "total": {"USDT": 1000.0}, "free": {}}


def test_futures_bot_ignores_positions_it_did_not_open(make_bot, monkeypatch):
    fx = FakeFut(np.full(400, 100.0), foreign_short=2.0)   # flat market -> no signal
    monkeypatch.setattr(live, "make_exchange", lambda s, authenticated=None: fx)
    s = Settings(mode="demo", api_key="k", api_secret="s", market="futures", symbols=["BTC/USDT"],
                 btc_filter=False).validate()
    bot = live.Bot(s)
    bot.run(once=True)
    assert bot.trades == {}                                  # not adopted
    assert not any("stopLossPrice" in o[3] for o in fx.orders)   # no stop put on the foreign short
    assert "BTC/USDT:USDT" in bot.state["warned"]


# ------------------------------------------------------------------ carry hedging
class CarryFake:
    def __init__(self, fail=False):
        self.fail, self.orders = fail, []
    def amount_to_precision(self, s, q): return f"{q:.4f}"
    def fetch_ticker(self, s): return {"last": 100.0}
    def load_markets(self): return {}
    def set_margin_mode(self, *a): pass
    def set_leverage(self, *a): pass
    def create_order(self, s, typ, side, qty, price=None, params=None):
        if self.fail:
            raise ccxt.InsufficientFunds("no USDT in spot wallet")
        self.orders.append((s, side, qty, params or {}))
        return {"status": "closed", "filled": qty, "average": 100.0}


def test_carry_never_left_unhedged_when_spot_leg_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(carry, "STATE_DIR", tmp_path)
    s = Settings(mode="demo", api_key="k", api_secret="s", carry_capital=300).validate()
    eng = carry.CarryEngine(s)
    eng.spot, eng.fut = CarryFake(fail=True), CarryFake()
    with pytest.raises(ccxt.InsufficientFunds):
        eng.open_pair("BTC")
    sides = [(o[1], o[3].get("reduceOnly", False)) for o in eng.fut.orders]
    assert sides == [("sell", False), ("buy", True)]      # short opened, then closed again
    assert eng.pairs == {}


def test_report_builds_from_journal(make_bot, monkeypatch):
    import tradingbot.report as rep
    fx = FakeSpot(UP)
    bot = make_bot(fx)
    bot.journal.every = 0
    for _ in range(3):
        fx.step(fx.closes[-1] * 1.01)
        bot.run(once=True)
    monkeypatch.setattr(rep, "STATE_DIR", live.STATE_DIR)
    monkeypatch.setattr(rep, "OUT_DIR", live.STATE_DIR / "out")
    html = rep.build_report().read_text()
    assert "DEMO" in html and "BTC/USDT" in html and "<svg" in html
