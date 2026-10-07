"""`python -m tradingbot report` -> reports/report.html: one self-contained page per running bot.

Reads only local files written by the bot (state/*.csv, state/*.json). No network, no keys.
Open the HTML in any browser; it works offline and follows the system light/dark setting.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "state"
OUT_DIR = PROJECT_ROOT / "reports"

# What the backtest said to expect on data that was never used for tuning (2025-01..2026-10).
EXPECTED = {
    "spot": "Saklı test dönemi (balanced): yıllık ~%9.5, en kötü düşüş -%14 civarı, isabet ~%20",
    "futures": "Saklı test dönemi (balanced): yıllık ~%8.9, en kötü düşüş -%12 civarı, isabet ~%25",
}

CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;
--text-secondary:#52514e;--muted:#898781;--grid:#e1e0d9;--series-1:#2a78d6;--good:#006300;
--critical:#d03b3b;--border:#e1e0d9}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--series-1:#3987e5;--good:#0ca30c;--critical:#e66767;--border:#2c2c2a}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--series-1:#3987e5;--good:#0ca30c;--critical:#e66767;
--border:#2c2c2a}
*{box-sizing:border-box}body{margin:0}
.viz-root{background:var(--page);color:var(--text-primary);font:14px/1.45 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,sans-serif;min-height:100vh;padding:24px 16px}
.wrap{max-width:1040px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.sub{color:var(--text-secondary);margin:0 0 20px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.tile .k{color:var(--text-secondary);font-size:12px}.tile .v{font-size:22px;font-weight:600;
font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--critical)}
.note{color:var(--text-secondary);font-size:13px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--text-secondary);font-weight:500;font-size:12px}
.scroll{overflow-x:auto}
.chart{position:relative}.chart svg{display:block;width:100%;height:auto}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
border-radius:8px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;
white-space:nowrap}
.tip b{font-variant-numeric:tabular-nums}
"""

JS = """
document.querySelectorAll('.chart').forEach(function(c){
  var d=JSON.parse(c.dataset.points), svg=c.querySelector('svg'), tip=c.querySelector('.tip'),
      hl=svg.querySelector('.hl'), dot=svg.querySelector('.dot'), W=+c.dataset.w;
  function show(ev){
    var r=svg.getBoundingClientRect(), x=(ev.clientX-r.left)*W/r.width, best=0, bd=1e9;
    for(var i=0;i<d.length;i++){var dd=Math.abs(d[i][0]-x); if(dd<bd){bd=dd;best=i;}}
    var p=d[best]; hl.setAttribute('x1',p[0]);hl.setAttribute('x2',p[0]);hl.style.display='';
    dot.setAttribute('cx',p[0]);dot.setAttribute('cy',p[1]);dot.style.display='';
    tip.innerHTML=p[2]+'<br><b>'+p[3]+' USDT</b>'; tip.style.display='block';
    var px=p[0]*r.width/W; tip.style.left=Math.min(Math.max(px-60,0),r.width-140)+'px'; tip.style.top='4px';
  }
  svg.addEventListener('mousemove',show);
  svg.addEventListener('mouseleave',function(){tip.style.display='none';hl.style.display='none';dot.style.display='none';});
});
"""


def _fmt(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}"


def _signed_cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def equity_svg(eq: pd.DataFrame, w: int = 1000, h: int = 280) -> str:
    """Single-series line chart (no legend needed; the heading names it) with a hover crosshair."""
    if len(eq) < 2:
        return "<p class='note'>Özsermaye eğrisi için en az iki kayıt gerekiyor (bot saatte bir kaydediyor).</p>"
    pad_l, pad_r, pad_t, pad_b = 64, 12, 12, 28
    t = pd.to_datetime(eq["time"], utc=True, format="mixed")
    v = eq["equity"].astype(float)
    t0, t1 = t.min().timestamp(), t.max().timestamp()
    lo, hi = float(v.min()), float(v.max())
    span = (hi - lo) or max(hi * 0.01, 1)
    lo, hi = lo - span * 0.08, hi + span * 0.08
    X = lambda ts: pad_l + (ts - t0) / max(t1 - t0, 1) * (w - pad_l - pad_r)
    Y = lambda y: pad_t + (hi - y) / (hi - lo) * (h - pad_t - pad_b)
    pts = [(round(X(a.timestamp()), 1), round(Y(b), 1), a.strftime("%d.%m %H:%M"), _fmt(b))
           for a, b in zip(t, v)]
    path = "M" + " L".join(f"{x},{y}" for x, y, *_ in pts)
    grid = []
    for i in range(5):
        y_val = lo + (hi - lo) * i / 4
        y = Y(y_val)
        grid.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y:.1f}' y2='{y:.1f}' stroke='var(--grid)' "
                    f"stroke-width='1'/><text x='{pad_l - 8}' y='{y + 4:.1f}' text-anchor='end' "
                    f"fill='var(--muted)' font-size='11'>{_fmt(y_val, 0)}</text>")
    for frac in (0, 0.5, 1):
        ts = t0 + (t1 - t0) * frac
        lab = datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y")
        anchor = "start" if frac == 0 else ("end" if frac == 1 else "middle")
        grid.append(f"<text x='{X(ts):.1f}' y='{h - 8}' text-anchor='{anchor}' fill='var(--muted)' "
                    f"font-size='11'>{lab}</text>")
    start = float(v.iloc[0])
    base = f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{Y(start):.1f}' y2='{Y(start):.1f}' " \
           f"stroke='var(--muted)' stroke-dasharray='3 4' stroke-width='1'/>"
    return (f"<div class='chart' data-w='{w}' data-points='{html.escape(json.dumps(pts))}'>"
            f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Özsermaye eğrisi'>{''.join(grid)}{base}"
            f"<path d='{path}' fill='none' stroke='var(--series-1)' stroke-width='2' "
            f"stroke-linejoin='round' stroke-linecap='round'/>"
            f"<line class='hl' y1='{pad_t}' y2='{h - pad_b}' stroke='var(--muted)' stroke-width='1' "
            f"style='display:none'/><circle class='dot' r='4' fill='var(--series-1)' "
            f"stroke='var(--surface-1)' stroke-width='2' style='display:none'/>"
            f"<rect x='0' y='0' width='{w}' height='{h}' fill='transparent'/></svg>"
            f"<div class='tip'></div></div>")


def _table(rows: list[list[str]], head: list[str]) -> str:
    if not rows:
        return "<p class='note'>Kayıt yok.</p>"
    th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"


def section_for(tag: str) -> str:
    mode, market = tag.split("_", 1)
    eq_path, j_path = STATE_DIR / f"equity_{tag}.csv", STATE_DIR / f"journal_{tag}.csv"
    bot_path, risk_path = STATE_DIR / f"bot_{tag}.json", STATE_DIR / f"risk_{tag}.json"
    eq = pd.read_csv(eq_path) if eq_path.exists() else pd.DataFrame(columns=["time", "equity"])
    j = pd.read_csv(j_path) if j_path.exists() else pd.DataFrame()
    bot = json.loads(bot_path.read_text()) if bot_path.exists() else {}
    risk = json.loads(risk_path.read_text()) if risk_path.exists() else {}

    tiles = []
    if len(eq):
        first, last = float(eq["equity"].iloc[0]), float(eq["equity"].iloc[-1])
        peak = float(eq["equity"].astype(float).cummax().iloc[-1])
        dd = (eq["equity"].astype(float) / eq["equity"].astype(float).cummax() - 1).min() * 100
        ret = (last / first - 1) * 100 if first else 0.0
        days = (pd.to_datetime(eq["time"].iloc[-1], utc=True) - pd.to_datetime(eq["time"].iloc[0], utc=True)).days
        tiles += [("Özsermaye", f"{_fmt(last)} USDT", ""),
                  ("Toplam getiri", f"{ret:+.2f}%", _signed_cls(ret)),
                  ("Zirveden şu an", f"{(last / peak - 1) * 100:+.2f}%", _signed_cls(last - peak)),
                  ("En kötü düşüş", f"{dd:.2f}%", "neg" if dd < 0 else ""),
                  ("Süre", f"{days} gün", "")]
    closes = j[j["action"] == "close"] if len(j) else pd.DataFrame()
    if len(closes):
        pnl = closes["pnl_est"].astype(float)
        wins = int((pnl > 0).sum())
        tiles += [("Kapanan işlem", str(len(closes)), ""),
                  ("İsabet", f"%{wins / len(closes) * 100:.0f}", ""),
                  ("Gerçekleşen PnL (tahmini)", f"{pnl.sum():+.2f} USDT", _signed_cls(pnl.sum()))]
    trades = bot.get("trades", {})
    tiles.append(("Açık pozisyon", str(len(trades)), ""))
    tile_html = "".join(f"<div class='tile'><div class='k'>{html.escape(k)}</div>"
                        f"<div class='v {c}'>{html.escape(v)}</div></div>" for k, v, c in tiles)

    halted = ""
    if risk.get("halted"):
        halted = (f"<div class='card'><b class='neg'>⛔ Bot durduruldu (kill switch):</b> "
                  f"{html.escape(risk.get('halt_reason', ''))}<br><span class='note'>İnceledikten sonra "
                  f"<code>python -m tradingbot reset-halt</code></span></div>")

    open_rows = [[html.escape(s), "LONG" if t["direction"] == 1 else "SHORT", f"{float(t['qty']):.6g}",
                  f"{float(t['entry']):.6g}", f"{float(t['stop']):.6g}",
                  f"{(float(t['stop']) / float(t['entry']) - 1) * 100:+.1f}%",
                  "✅" if t.get("stop_ok", True) else "⚠️ bot-side",
                  html.escape(str(t.get("opened", ""))[:16].replace("T", " "))] for s, t in trades.items()]
    recent = []
    if len(j):
        for _, r in j.iloc[::-1].head(50).iterrows():
            p = float(r.get("pnl_est") or 0)
            recent.append([html.escape(str(r["time"])[:16].replace("T", " ")), html.escape(str(r["symbol"])),
                           "AÇ" if r["action"] == "open" else "KAPAT",
                           "LONG" if int(r["direction"]) == 1 else "SHORT",
                           f"{float(r['price']):.6g}", html.escape(str(r["reason"])),
                           f"<span class='{_signed_cls(p)}'>{p:+.2f}</span>" if r["action"] == "close" else ""])
    return f"""
<h2>{html.escape(mode.upper())} · {html.escape(market)}</h2>
{halted}
<div class="tiles">{tile_html}</div>
<div class="card"><div class="note" style="margin-bottom:8px">Özsermaye (USDT) · kesikli çizgi: başlangıç</div>
{equity_svg(eq)}</div>
<div class="card"><div class="note">Beklenti: {html.escape(EXPECTED.get(market, ''))}. Birkaç hafta
backtest'ten sapma normaldir; aylarca çok daha kötüyse durup incele.</div></div>
<div class="card"><b>Açık pozisyonlar</b>{_table(open_rows, ['Coin', 'Yön', 'Miktar', 'Giriş', 'Stop',
 'Stop mesafesi', 'Borsa stop', 'Açılış (UTC)'])}</div>
<div class="card"><b>Son işlemler</b>{_table(recent, ['Zaman (UTC)', 'Coin', 'İşlem', 'Yön', 'Fiyat',
 'Sebep', 'PnL (USDT)'])}</div>"""


def carry_section() -> str:
    out = []
    for f in sorted(STATE_DIR.glob("carry_*.json")):
        st = json.loads(f.read_text())
        mode = f.stem.split("_", 1)[1]
        rows = [[html.escape(b), f"{float(p['qty']):.6g}", f"{float(p.get('spot_entry', 0)):.6g}",
                 f"{float(p.get('perp_entry', 0)):.6g}", html.escape(str(p.get('opened', ''))[:16])]
                for b, p in st.get("pairs", {}).items()]
        extra = ""
        if mode == "paper":
            extra = (f"<p class='note'>Paper nakit: {_fmt(float(st.get('paper_cash', 0)))} USDT · "
                     f"toplanan funding: {_fmt(float(st.get('paper_funding', 0)), 4)} USDT</p>")
        out.append(f"<h2>CARRY · {html.escape(mode)}</h2><div class='card'>{extra}"
                   f"<p class='note'>Son funding penceresi: {html.escape(st.get('last_window', '-'))}</p>"
                   f"{_table(rows, ['Coin', 'Miktar', 'Spot giriş', 'Perp giriş', 'Açılış'])}</div>")
    return "".join(out)


def build_report() -> Path:
    tags = sorted({p.stem.split("_", 1)[1] for p in STATE_DIR.glob("bot_*.json")} |
                  {p.stem.split("_", 1)[1] for p in STATE_DIR.glob("equity_*.csv")})
    body = "".join(section_for(t) for t in tags) + carry_section()
    if not body:
        body = "<div class='card'>Henüz çalışan bot yok. Önce <code>python -m tradingbot run</code>.</div>"
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    page = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TradingBot Raporu</title>
<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">
<h1>TradingBot raporu</h1><p class="sub">Oluşturuldu: {now} · veriler: state/ klasörü · tahmini PnL ücretleri
içerir, kesin değerler için Binance işlem geçmişine bak.</p>{body}
<p class="note" style="margin-top:24px">Geçmiş sonuçlar geleceği garanti etmez. Bu bir yatırım tavsiyesi değildir.</p>
</div></div><script>{JS}</script></body></html>"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "report.html"
    out.write_text(page, encoding="utf-8")
    return out
