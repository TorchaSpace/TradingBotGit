"""Settings loaded from environment / .env. Live trading is opt-in only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISK"
VALID_MODES = ("paper", "demo", "live")
VALID_MARKETS = ("spot", "futures")

# Varsayılan ücretler (VIP0, BNB indirimi yok). Kendi seviyene göre .env'den değiştirebilirsin.
DEFAULT_FEES = {"spot": 0.001, "futures": 0.0005}

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
                   "XRP/USDT", "ADA/USDT", "LINK/USDT", "DOGE/USDT"]

CARRY_DEFAULT_BASES = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "LINK", "DOGE", "DOT", "LTC", "TRX",
                       "AVAX", "ATOM", "UNI", "FIL", "ETC", "XLM", "NEAR", "AAVE", "BCH", "ALGO",
                       "VET", "HBAR"]

# Risk profilleri, 8 coinlik portföy backtest'inden (4h, 2021-2026, research/ klasörü).
# .env'de tek tek yazılan ayarlar profili ezer.
PROFILES = {
    "conservative": dict(risk_per_trade=0.0025, atr_stop_mult=5.0, max_open_positions=6,
                         max_daily_loss=0.03, max_drawdown=0.15),
    "balanced": dict(risk_per_trade=0.005, atr_stop_mult=5.0, max_open_positions=6,
                     max_daily_loss=0.05, max_drawdown=0.25),
    "aggressive": dict(risk_per_trade=0.0075, atr_stop_mult=4.0, max_open_positions=6,
                       max_daily_loss=0.07, max_drawdown=0.35),
}


class ConfigError(ValueError):
    pass


def _num(raw: str | None, default, cast):
    raw = (raw or "").strip()
    return cast(raw) if raw else default


def _bool(raw: str | None, default: bool) -> bool:
    raw = (raw or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _list(raw: str | None, default: list[str]) -> list[str]:
    raw = (raw or "").strip()
    return [x.strip().upper() for x in raw.split(",") if x.strip()] if raw else list(default)


@dataclass
class Settings:
    mode: str = "paper"
    market: str = "spot"
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    live_confirm: str = field(default="", repr=False)
    strategy: str = "ema_trend"
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    timeframe: str = "4h"
    profile: str = "balanced"
    risk_per_trade: float = 0.005
    atr_stop_mult: float = 5.0
    trailing: bool = False
    btc_filter: bool = True
    max_daily_loss: float = 0.05
    max_drawdown: float = 0.25
    max_open_positions: int = 6
    max_leverage: float = 3.0
    paper_start_balance: float = 1000.0
    fee_rate: float | None = None
    slippage: float = 0.0005
    bnb_fee_discount: bool = False      # pay fees in BNB (-25%); enable it in Binance too
    maker_first: bool = False           # try a post-only limit order first, fall back to market
    maker_wait_seconds: int = 45
    learner_mode: str = "shadow"         # off | shadow (score only) | filter (skip weakest signals, if approved)
    # ---- funding carry (spot long + perp short), separate engine: python -m tradingbot carry-run
    carry_capital: float = 0.0          # USDT reserved for the carry engine (0 = off)
    carry_symbols: list[str] = field(default_factory=lambda: list(CARRY_DEFAULT_BASES))
    carry_slots: int = 5
    carry_leverage: float = 2.0
    carry_lookback: int = 9             # funding periods (8h) averaged
    carry_enter: float = 0.0001         # enter if avg funding per 8h > 0.01% (~11%/yr)
    carry_exit: float = 0.0             # exit if avg funding turns negative

    @property
    def fee(self) -> float:
        base = self.fee_rate if self.fee_rate is not None else DEFAULT_FEES[self.market]
        return base * (0.75 if self.bnb_fee_discount else 1.0)

    @property
    def leverage_cap(self) -> float:
        return 1.0 if self.market == "spot" else self.max_leverage

    @property
    def btc_symbol(self) -> str:
        return "BTC/USDT:USDT" if self.market == "futures" else "BTC/USDT"

    @property
    def allow_short(self) -> bool:
        return self.market == "futures"

    def validate(self) -> "Settings":
        if self.mode not in VALID_MODES:
            raise ConfigError(f"MODE must be one of {VALID_MODES}, got {self.mode!r}")
        if self.market not in VALID_MARKETS:
            raise ConfigError(f"MARKET must be one of {VALID_MARKETS}, got {self.market!r}")
        if self.learner_mode not in ("off", "shadow", "filter"):
            raise ConfigError(f"LEARNER_MODE must be off, shadow or filter, got {self.learner_mode!r}")
        # futures (USDⓈ-M perpetual) symbols in ccxt look like BTC/USDT:USDT; spot like BTC/USDT
        if self.market == "futures":
            self.symbols = [x if ":" in x else f"{x}:{x.split('/')[1]}" for x in self.symbols]
        else:
            self.symbols = [x.split(":")[0] for x in self.symbols]
        if self.mode == "live" and self.live_confirm != LIVE_CONFIRM_PHRASE:
            raise ConfigError(
                "MODE=live ama LIVE_TRADING_CONFIRM ayarlanmamış. Gerçek parayla işlem için "
                f".env içinde LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE} yazmalısın."
            )
        if self.mode in ("demo", "live") and not (self.api_key and self.api_secret):
            raise ConfigError(f"{self.mode} modu için API anahtarı ve secret gerekli "
                              f"(BINANCE_{self.mode.upper()}_API_KEY / _API_SECRET).")
        if self.profile not in PROFILES:
            raise ConfigError(f"PROFILE must be one of {tuple(PROFILES)}, got {self.profile!r}")
        if not 0 < self.risk_per_trade <= 0.05:
            raise ConfigError("RISK_PER_TRADE 0 ile 0.05 (%5) arasında olmalı.")
        if not 0 < self.max_daily_loss <= 0.2:
            raise ConfigError("MAX_DAILY_LOSS 0 ile 0.2 arasında olmalı.")
        if not 0 < self.max_drawdown <= 0.5:
            raise ConfigError("MAX_DRAWDOWN 0 ile 0.5 arasında olmalı.")
        if not 1 <= self.carry_leverage <= 3:
            raise ConfigError("CARRY_LEVERAGE 1 ile 3 arasında olmalı (short tarafın likidasyon riski).")
        if self.carry_capital < 0 or self.carry_slots < 1:
            raise ConfigError("CARRY_CAPITAL >= 0 ve CARRY_SLOTS >= 1 olmalı.")
        if not 1 <= self.max_leverage <= 10:
            raise ConfigError("MAX_LEVERAGE 1 ile 10 arasında olmalı (bot daha yükseğine izin vermez).")
        return self


def settings_from(values: Mapping[str, str]) -> Settings:
    """Build + validate Settings from a dict of .env-style strings (missing keys -> defaults/profile)."""
    g = lambda k, d="": (values.get(k) if values.get(k) is not None else d)
    mode = g("MODE", "paper").strip().lower() or "paper"
    profile = g("PROFILE", "balanced").strip().lower() or "balanced"
    pr = PROFILES.get(profile, PROFILES["balanced"])
    # separate key slots for the demo and the real account; BINANCE_API_KEY is the old single slot
    slot = {"demo": "BINANCE_DEMO_", "live": "BINANCE_LIVE_"}.get(mode, "")
    key = (g(slot + "API_KEY") if slot else "") or g("BINANCE_API_KEY")
    secret = (g(slot + "API_SECRET") if slot else "") or g("BINANCE_API_SECRET")
    s = Settings(
        mode=mode,
        market=g("MARKET", "spot").strip().lower() or "spot",
        api_key=key.strip(),
        api_secret=secret.strip(),
        live_confirm=g("LIVE_TRADING_CONFIRM").strip(),
        strategy=g("STRATEGY", "ema_trend").strip() or "ema_trend",
        symbols=_list(g("SYMBOLS"), DEFAULT_SYMBOLS),
        timeframe=g("TIMEFRAME", "4h").strip() or "4h",
        profile=profile,
        risk_per_trade=_num(g("RISK_PER_TRADE"), pr["risk_per_trade"], float),
        atr_stop_mult=_num(g("ATR_STOP_MULT"), pr["atr_stop_mult"], float),
        trailing=_bool(g("TRAILING_STOP"), False),
        btc_filter=_bool(g("BTC_FILTER"), True),
        max_daily_loss=_num(g("MAX_DAILY_LOSS"), pr["max_daily_loss"], float),
        max_drawdown=_num(g("MAX_DRAWDOWN"), pr["max_drawdown"], float),
        max_open_positions=_num(g("MAX_OPEN_POSITIONS"), pr["max_open_positions"], int),
        max_leverage=_num(g("MAX_LEVERAGE"), 3.0, float),
        paper_start_balance=_num(g("PAPER_START_BALANCE"), 1000.0, float),
        fee_rate=_num(g("FEE_RATE"), None, float),
        slippage=_num(g("SLIPPAGE"), 0.0005, float),
        bnb_fee_discount=_bool(g("BNB_FEE_DISCOUNT"), False),
        maker_first=_bool(g("MAKER_FIRST"), False),
        maker_wait_seconds=_num(g("MAKER_WAIT_SECONDS"), 45, int),
        learner_mode=(g("LEARNER_MODE") or "shadow").strip().lower(),
        carry_capital=_num(g("CARRY_CAPITAL"), 0.0, float),
        carry_symbols=_list(g("CARRY_SYMBOLS"), CARRY_DEFAULT_BASES),
        carry_slots=_num(g("CARRY_SLOTS"), 5, int),
        carry_leverage=_num(g("CARRY_LEVERAGE"), 2.0, float),
        carry_lookback=_num(g("CARRY_LOOKBACK"), 9, int),
        carry_enter=_num(g("CARRY_ENTER"), 0.0001, float),
        carry_exit=_num(g("CARRY_EXIT"), 0.0, float),
    )
    return s.validate()


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """CLI path: .env file + real environment variables (environment wins)."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    return settings_from(os.environ)
