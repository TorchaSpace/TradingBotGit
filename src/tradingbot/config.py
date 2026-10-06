"""Settings loaded from environment / .env. Live trading is opt-in only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    return float(raw) if raw.strip() else default


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    return int(raw) if raw.strip() else default


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
            raise ConfigError(f"MODE={self.mode} için BINANCE_API_KEY ve BINANCE_API_SECRET gerekli.")
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


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    fee_raw = os.getenv("FEE_RATE", "").strip()
    profile = os.getenv("PROFILE", "balanced").strip().lower()
    pr = PROFILES.get(profile, PROFILES["balanced"])
    s = Settings(
        mode=os.getenv("MODE", "paper").strip().lower(),
        market=os.getenv("MARKET", "spot").strip().lower(),
        api_key=os.getenv("BINANCE_API_KEY", "").strip(),
        api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        live_confirm=os.getenv("LIVE_TRADING_CONFIRM", "").strip(),
        strategy=os.getenv("STRATEGY", "ema_trend").strip(),
        symbols=[x.strip().upper() for x in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",")
                 if x.strip()],
        timeframe=os.getenv("TIMEFRAME", "4h").strip(),
        profile=profile,
        risk_per_trade=_f("RISK_PER_TRADE", pr["risk_per_trade"]),
        atr_stop_mult=_f("ATR_STOP_MULT", pr["atr_stop_mult"]),
        trailing=_b("TRAILING_STOP", False),
        btc_filter=_b("BTC_FILTER", True),
        max_daily_loss=_f("MAX_DAILY_LOSS", pr["max_daily_loss"]),
        max_drawdown=_f("MAX_DRAWDOWN", pr["max_drawdown"]),
        max_open_positions=_i("MAX_OPEN_POSITIONS", pr["max_open_positions"]),
        max_leverage=_f("MAX_LEVERAGE", 3.0),
        paper_start_balance=_f("PAPER_START_BALANCE", 1000.0),
        fee_rate=float(fee_raw) if fee_raw else None,
        slippage=_f("SLIPPAGE", 0.0005),
        bnb_fee_discount=_b("BNB_FEE_DISCOUNT", False),
        maker_first=_b("MAKER_FIRST", False),
        maker_wait_seconds=_i("MAKER_WAIT_SECONDS", 45),
        carry_capital=_f("CARRY_CAPITAL", 0.0),
        carry_symbols=[x.strip().upper() for x in os.getenv("CARRY_SYMBOLS", ",".join(CARRY_DEFAULT_BASES))
                       .split(",") if x.strip()],
        carry_slots=_i("CARRY_SLOTS", 5),
        carry_leverage=_f("CARRY_LEVERAGE", 2.0),
        carry_lookback=_i("CARRY_LOOKBACK", 9),
        carry_enter=_f("CARRY_ENTER", 0.0001),
        carry_exit=_f("CARRY_EXIT", 0.0),
    )
    return s.validate()
