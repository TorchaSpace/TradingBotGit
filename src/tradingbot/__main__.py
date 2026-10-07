"""Command line entry point.

    python -m tradingbot compare   --market spot --symbols BTC/USDT,ETH/USDT --since 2021-01-01
    python -m tradingbot backtest  --strategy ema_trend --symbols BTC/USDT --since 2021-01-01
    python -m tradingbot check     # test API connection / show balance
    python -m tradingbot run       # start bot (MODE from .env: paper | demo | live)
    python -m tradingbot flatten   # close all bot positions now
    python -m tradingbot reset-halt
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

import pandas as pd

from .backtest import BacktestParams, run_backtest, run_portfolio
from .config import PROJECT_ROOT, ConfigError, load_settings
from .data import load_history
from .strategies import STRATEGIES, get_strategy, make_target

RESULTS_DIR = PROJECT_ROOT / "backtests" / "results"
LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging(verbose: bool = False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")])
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _bar_hours(tf: str) -> float:
    unit, n = tf[-1], float(tf[:-1])
    return n * {"m": 1 / 60, "h": 1, "d": 24, "w": 168}[unit]


def _params(s, tf: str) -> BacktestParams:
    return BacktestParams(
        start_equity=s.paper_start_balance, fee=s.fee, slippage=s.slippage,
        risk_per_trade=s.risk_per_trade, atr_mult=s.atr_stop_mult, trailing=s.trailing,
        leverage_cap=s.leverage_cap, allow_short=s.allow_short,
        funding_rate_8h=0.0001 if s.market == "futures" else 0.0,
        bar_hours=_bar_hours(tf),
    )


def _apply_overrides(s, a):
    if getattr(a, "market", None):
        s.market = a.market
    if getattr(a, "symbols", None):
        s.symbols = [x.strip().upper() for x in a.symbols.split(",")]
    if getattr(a, "timeframe", None):
        s.timeframe = a.timeframe
    if getattr(a, "strategy", None):
        s.strategy = a.strategy
    if getattr(a, "profile", None):
        from .config import PROFILES
        s.profile = a.profile
        for k, v in PROFILES[a.profile].items():
            setattr(s, k, v)
    return s.validate()


def _btc(s, since, until):
    return load_history(s, s.btc_symbol, s.timeframe, since, until) if s.btc_filter else None


def _fmt_table(rows: list[dict]) -> str:
    df = pd.DataFrame(rows)
    cols = ["symbol", "strategy", "period", "total_return_%", "buy_hold_%", "cagr_%", "max_drawdown_%",
            "sharpe", "trades", "win_rate_%", "profit_factor", "avg_win_loss_ratio", "exposure_%"]
    df = df[[c for c in cols if c in df.columns]]
    return df.to_string(index=False, float_format=lambda x: f"{x:,.2f}")


def cmd_compare(s, a):
    rows = []
    names = a.strategies.split(",") if a.strategies else list(STRATEGIES)
    p = _params(s, s.timeframe)
    btc = _btc(s, a.since, a.until)
    for sym in s.symbols:
        df = load_history(s, sym, s.timeframe, a.since, a.until)
        print(f"{sym}: {len(df)} bars {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
        split = int(len(df) * 0.7)
        for name in names:
            target = make_target(name, df, s.allow_short, btc)
            for period, sl in (("ALL", slice(None)), ("IN 70%", slice(None, split)),
                               ("OUT 30%", slice(split, None))):
                # strategy computed on full history (indicators warm), engine run on the slice
                part = df.iloc[sl]
                res = run_backtest(part, target.loc[part.index], p)
                rows.append({"symbol": sym, "strategy": name, "period": period, **res.metrics})
    print()
    print(f"BTC filter={'on' if s.btc_filter else 'off'}  Market={s.market}  TF={s.timeframe}  fee={s.fee:.4%}  slippage={s.slippage:.4%}  "
          f"risk/trade={s.risk_per_trade:.1%}  ATR stop x{s.atr_stop_mult}  "
          f"shorts={'on' if s.allow_short else 'off'}  start={s.paper_start_balance:.0f} USDT")
    print(_fmt_table(rows))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"compare_{s.market}_{s.timeframe}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\nSaved: {out.relative_to(PROJECT_ROOT)}")
    print("\nNot: Geçmiş performans gelecekteki sonuçları garanti etmez. 'OUT 30%' satırları, "
          "stratejinin görmediği dönemdir; asıl dikkate alınması gereken odur.")


def cmd_backtest(s, a):
    spec = get_strategy(s.strategy)
    p = _params(s, s.timeframe)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    btc = _btc(s, a.since, a.until)
    for sym in s.symbols:
        df = load_history(s, sym, s.timeframe, a.since, a.until)
        res = run_backtest(df, make_target(spec.name, df, s.allow_short, btc), p)
        print(f"\n=== {sym} {spec.name} ({s.market}, {s.timeframe}) ===")
        for k, v in res.metrics.items():
            print(f"  {k:20s} {v:,.2f}" if isinstance(v, float) else f"  {k:20s} {v}")
        tag = f"{s.market}_{sym.replace('/', '')}_{spec.name}_{s.timeframe}"
        res.trades.to_csv(RESULTS_DIR / f"trades_{tag}.csv", index=False)
        res.equity.to_csv(RESULTS_DIR / f"equity_{tag}.csv")
        print(f"  trades/equity saved to backtests/results/*_{tag}.csv")


def cmd_portfolio(s, a):
    """All symbols in ONE shared account, exactly like the live bot (risk %, max positions)."""
    spec = get_strategy(s.strategy)
    p = _params(s, s.timeframe)
    dfs, tg = {}, {}
    btc = _btc(s, a.warmup_since, a.until)
    for sym in s.symbols:
        warm = load_history(s, sym, s.timeframe, a.warmup_since, a.until)
        df = warm[warm.index >= pd.Timestamp(a.since, tz="UTC")]
        if len(df) < 50:
            print(f"  {sym}: not enough data, skipped")
            continue
        dfs[sym], tg[sym] = df, make_target(spec.name, warm, s.allow_short, btc).loc[df.index]
    res = run_portfolio(dfs, tg, p, max_positions=s.max_open_positions)
    m = res.metrics
    e = res.equity
    print(f"\n=== PORTFOLIO {spec.name} | {s.market} {s.timeframe} | profile={s.profile} "
          f"risk={s.risk_per_trade:.2%} stop={s.atr_stop_mult}xATR trailing={s.trailing} "
          f"max_pos={s.max_open_positions} btc_filter={s.btc_filter} | {len(dfs)} symbols ===")
    for k in ("total_return_%", "cagr_%", "max_drawdown_%", "sharpe", "sortino", "trades", "win_rate_%",
              "profit_factor", "avg_win_loss_ratio"):
        v = m[k]
        print(f"  {k:20s} {v:,.2f}" if isinstance(v, float) else f"  {k:20s} {v}")
    yearly = (e.resample("YE").last() / e.resample("YE").first() - 1) * 100
    print("  yearly %: " + "  ".join(f"{d.year}: {v:+.1f}" for d, v in yearly.items()))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"portfolio_{s.market}_{spec.name}_{s.profile}_{s.timeframe}"
    res.trades.to_csv(RESULTS_DIR / f"trades_{tag}.csv", index=False)
    e.to_csv(RESULTS_DIR / f"equity_{tag}.csv")
    print(f"  saved backtests/results/*_{tag}.csv")
    print("\nNot: Geçmiş performans gelecekteki sonuçları garanti etmez.")


def cmd_carry_backtest(s, a):
    from .carry import CarryParams, carry_backtest, prices_8h
    from .config import Settings as _S
    from .data import load_funding
    F, S, P = {}, {}, {}
    for b in s.carry_symbols:
        try:
            F[b] = load_funding(b)
            S[b] = prices_8h(load_history(_S(market="spot", symbols=[f"{b}/USDT"]).validate(),
                                          f"{b}/USDT", "4h", "2019-09-01")["close"])
            P[b] = prices_8h(load_history(_S(market="futures", symbols=[f"{b}/USDT"]).validate(),
                                          f"{b}/USDT:USDT", "4h", "2019-09-01")["close"])
        except Exception as e:
            print(f"  {b}: skipped ({type(e).__name__})")
    cp = CarryParams(slots=s.carry_slots, leverage=s.carry_leverage, lookback=s.carry_lookback,
                     enter=s.carry_enter, exit=s.carry_exit)
    print(f"\n=== FUNDING CARRY | {len(F)} coins | slots={cp.slots} lev={cp.leverage} "
          f"enter>{cp.enter:.5f}/8h exit<{cp.exit:.5f} lookback={cp.lookback} ===")
    for label, a0, a1 in (("ALL", a.since, None), ("2021-2024", "2021-01-01", "2025-01-01"),
                          ("2025-", "2025-01-01", None)):
        e, log_df, m = carry_backtest(F, S, P, cp, a0, a1)
        print(f"  {label:10s} CAGR {m['cagr_%']:6.2f}%  maxDD {m['max_drawdown_%']:6.2f}%  "
              f"Sharpe {m['sharpe']:5.2f}  rebalances {len(log_df)}")
        if label == "ALL":
            yearly = (e.resample("YE").last() / e.resample("YE").first() - 1) * 100
            print("  yearly %: " + "  ".join(f"{d.year}: {v:+.1f}" for d, v in yearly.items()))
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            e.to_csv(RESULTS_DIR / "equity_carry.csv")
            log_df.to_csv(RESULTS_DIR / "trades_carry.csv", index=False)
    print("\nNot: Likidasyon, baz kayması ve cüzdanlar arası transfer tam modellenmedi. Garanti değildir.")


def cmd_carry_run(s, a):
    from .carry import CarryEngine
    CarryEngine(s).run(once=a.once)


def cmd_carry_flatten(s, a):
    from .carry import CarryEngine
    CarryEngine(s).flatten()
    print("Carry pozisyonları kapatıldı.")


def cmd_report(s, a):
    from .report import build_report
    out = build_report()
    print(f"Rapor: {out}")
    if a.open:
        import webbrowser
        webbrowser.open(out.as_uri())


def cmd_validate(s, a):
    from .validation import load_for, run_validation, save
    print(f"Veri yükleniyor ({s.market}, {len(s.symbols)} coin)...")
    full, btc = load_for(s, a.warmup_since)
    if not full:
        print("Veri yüklenemedi (internet bağlantısını kontrol et).")
        return
    res = run_validation(s, full, btc, start=a.since, progress=lambda m: print(f"  · {m}"))
    j, h = save(res)
    print()
    for v in res["verdict"]:
        mark = {"ok": "✅", "warn": "⚠️ ", "info": "ℹ️ "}[v["state"]]
        print(f"{mark} {v['title']}\n   {v['text']}\n")
    print(f"Rapor: {h}")
    if a.open:
        import webbrowser
        webbrowser.open(h.as_uri())


def cmd_lookahead(s, a):
    from .lookahead import check_all
    from .validation import load_for
    print("Veri yükleniyor...")
    full, btc = load_for(s, "2020-01-01")
    if not full:
        print("Veri yüklenemedi.")
        return
    bad = 0
    for r in check_all(full, btc, s.allow_short):
        bad += not r["ok"]
        print(("✅" if r["ok"] else "❌"), r["what"], "" if r["ok"] else f"({r['mismatches']} değer gelecekten etkileniyor)")
    print("\nGeleceği görme hatası yok." if not bad else f"\n{bad} hesaplama geleceği görüyor: backtest sonuçlarına güvenme!")


def cmd_app(s, a):
    from .app.main import main as app_main
    return app_main(no_window=a.no_window, port=a.port)


def cmd_notify_test(s, a):
    from .notify import Notifier
    n = Notifier(prefix="[TradingBot]")
    if not n.enabled:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID .env içinde yok.")
        return
    print("Gönderildi ✅" if n.send("Test mesajı: bildirimler çalışıyor.", block=True) else "Gönderilemedi ❌")


def cmd_check(s, a):
    from .exchange import make_exchange
    from .live import ExchangeBroker, PaperBroker
    print(f"mode={s.mode} market={s.market} symbols={s.symbols}")
    ex = make_exchange(s)
    ex.load_markets()
    for sym in s.symbols:
        if sym not in ex.markets:
            print(f"  !! {sym} bu piyasada yok")
            continue
        print(f"  {sym}: last={ex.fetch_ticker(sym)['last']}")
    broker = PaperBroker(s, ex) if s.mode == "paper" else ExchangeBroker(s, ex)
    print(f"  equity (USDT) = {broker.equity():.2f}")
    print("Bağlantı OK.")


def cmd_run(s, a):
    from .live import Bot
    Bot(s).run(poll_seconds=a.poll, once=a.once)


def cmd_flatten(s, a):
    from .live import Bot
    Bot(s).flatten()
    print("Bot pozisyonları kapatıldı.")


def cmd_reset_halt(s, a):
    from .live import Bot
    b = Bot(s)
    b.risk.reset_halt()
    print("Halt kaldırıldı. Zirve özsermaye sıfırlandı.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tradingbot")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, hist=False):
        p.add_argument("--market", choices=["spot", "futures"])
        p.add_argument("--symbols")
        p.add_argument("--timeframe")
        if hist:
            p.add_argument("--since", default="2021-01-01")
            p.add_argument("--until")

    p = sub.add_parser("compare", help="compare all strategies on history")
    common(p, True)
    p.add_argument("--strategies", help="comma list, default all")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("backtest", help="backtest one strategy")
    common(p, True)
    p.add_argument("--strategy", choices=list(STRATEGIES))
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("portfolio", help="shared-account backtest of all symbols (like the live bot)")
    common(p, True)
    p.add_argument("--strategy", choices=list(STRATEGIES))
    p.add_argument("--profile", choices=["conservative", "balanced", "aggressive"])
    p.add_argument("--warmup-since", default="2020-06-01", help="extra history for indicator warm-up")
    p.set_defaults(fn=cmd_portfolio)

    p = sub.add_parser("carry-backtest", help="backtest the funding carry engine")
    p.add_argument("--since", default="2021-01-01")
    p.set_defaults(fn=cmd_carry_backtest)

    p = sub.add_parser("carry-run", help="run the funding carry engine (needs CARRY_CAPITAL)")
    p.add_argument("--once", action="store_true")
    p.set_defaults(fn=cmd_carry_run)

    p = sub.add_parser("carry-flatten", help="close all carry pairs")
    p.set_defaults(fn=cmd_carry_flatten)

    p = sub.add_parser("report", help="build reports/report.html from the bot's journal")
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("validate", help="robustness report: Monte Carlo, Deflated Sharpe, PBO, stress tests")
    common(p)
    p.add_argument("--strategy", choices=list(STRATEGIES))
    p.add_argument("--profile", choices=["conservative", "balanced", "aggressive"])
    p.add_argument("--since", default="2021-01-01")
    p.add_argument("--warmup-since", default="2020-01-01")
    p.add_argument("--open", action="store_true", help="open the report in the browser")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("lookahead", help="check every signal/feature for look-ahead bias")
    common(p)
    p.set_defaults(fn=cmd_lookahead)

    p = sub.add_parser("app", help="open the desktop app")
    p.add_argument("--no-window", action="store_true", help="open in the browser instead of a window")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("notify-test", help="send a Telegram test message")
    p.set_defaults(fn=cmd_notify_test)

    p = sub.add_parser("check", help="test connection and show equity")
    common(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("run", help="run the bot")
    common(p)
    p.add_argument("--strategy", choices=list(STRATEGIES))
    p.add_argument("--poll", type=int, default=30)
    p.add_argument("--once", action="store_true", help="single iteration (for testing)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("flatten", help="close all bot positions")
    common(p)
    p.set_defaults(fn=cmd_flatten)

    p = sub.add_parser("reset-halt", help="clear kill-switch after review")
    common(p)
    p.set_defaults(fn=cmd_reset_halt)

    a = ap.parse_args(argv)
    if a.cmd == "app":  # the app manages its own settings/logging and must open even with bad settings
        return cmd_app(None, a)
    setup_logging(a.verbose)
    try:
        s = _apply_overrides(load_settings(), a)
    except ConfigError as e:
        print(f"Ayar hatası: {e}")
        return 2
    a.fn(s, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
