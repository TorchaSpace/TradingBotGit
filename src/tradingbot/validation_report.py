"""reports/validation.html: the validation results as one self-contained, offline page."""
from __future__ import annotations

import html
import json
import math

from .report import CSS as BASE_CSS

PERIOD_NAMES = {"all": "Tümü (2021 → bugün)", "train": "Eğitim (2021-2024)", "holdout": "Saklı test (2025 → bugün)"}

# What was tried from the literature / practice and what happened (train 2021-24 AND holdout 2025-26).
LITERATURE = [
    ("Trend takibi, geniş stop (5×ATR), trailing yok", "Hurst, Ooi, Pedersen (2017)", "✅", "Bota alındı: en büyük etki"),
    ("BTC rejim filtresi", "Uygulama pratiği", "✅", "Bota alındı: saklı dönemde Sharpe 0.65 → 0.74"),
    ("Funding carry (spot long + perp short)", "Basis trade", "✅", "Ayrı motor; 2025-26'da getiri neredeyse sıfır"),
    ("BNB indirimi + maker emir", "Maliyet yönetimi", "✅", "Küçük ama garanti (+%1-1.5/yıl)"),
    ("Zaman serisi momentumu (TSMOM)", "Moskowitz, Ooi, Pedersen (2012)", "❌", "Mevcut trend kuralını geçemedi"),
    ("Volatiliteye göre risk ayarı", "Moreira, Muir (2017)", "➖", "Eğitimde Sharpe 1.89 vs 1.92 (sabit), saklıda 0.85 vs 0.75. Karışık, alınmadı"),
    ("Coinler arası momentum", "Jegadeesh, Titman (1993); Liu, Tsyvinski, Wu (2022)", "❌", "Düşüş -%55/-%70, saklıda kararsız"),
    ("Meta-labeling (ML ile sinyal filtreleme)", "López de Prado (2018)", "➖", "AUC 0.60-0.66 ama portföyde tutarlı fayda yok"),
    ("ML ile fiyat yönü tahmini", "-", "❌", "AUC 0.51 = yazı-tura"),
    ("Walk-forward yeniden optimizasyon", "Pardo (2008)", "❌", "Sabit ayardan kötü; PBO da bunu doğruluyor"),
    ("Kâr hedefi ile yüksek isabet", "-", "❌", "İsabet %70-90 ama getiri ~0"),
    ("Düşüşte riski yarıya indirme", "-", "❌", "Saklı dönemde kötü"),
    ("Parametre / strateji topluluğu", "-", "➖", "Tek ayardan iyi değil"),
]

REFERENCES = [
    "Bailey, D. & López de Prado, M. (2012). The Sharpe Ratio Efficient Frontier. <i>Journal of Risk</i> 15(2). (PSR)",
    "Bailey, D. & López de Prado, M. (2014). The Deflated Sharpe Ratio. <i>Journal of Portfolio Management</i> 40(5). (DSR)",
    "Bailey, D., Borwein, J., López de Prado, M. & Zhu, Q. (2017). The Probability of Backtest Overfitting. "
    "<i>Journal of Computational Finance</i> 20(4). (PBO / CSCV)",
    "Politis, D. & Romano, J. (1994). The Stationary Bootstrap. <i>JASA</i> 89(428). (Monte Carlo)",
    "Harvey, C., Liu, Y. & Zhu, H. (2016). … and the Cross-Section of Expected Returns. <i>RFS</i> 29(1). (çoklu test)",
    "White, H. (2000). A Reality Check for Data Snooping. <i>Econometrica</i> 68(5).",
    "Moskowitz, T., Ooi, Y. & Pedersen, L. (2012). Time Series Momentum. <i>JFE</i> 104(2).",
    "Hurst, B., Ooi, Y. & Pedersen, L. (2017). A Century of Evidence on Trend-Following Investing. <i>JPM</i> 44(1).",
    "Moreira, A. & Muir, T. (2017). Volatility-Managed Portfolios. <i>Journal of Finance</i> 72(4).",
    "López de Prado, M. (2018). <i>Advances in Financial Machine Learning</i>. Wiley.",
]

CSS = BASE_CSS + """
.viz-root{--bar-neg:#e34948;--band-outer:rgba(42,120,214,.16);--band-inner:rgba(42,120,214,.34);
--warning:#b07a00;--seq-1:#cde2fb;--seq-2:#9ec5f4;--seq-3:#6da7ec;--seq-4:#3987e5;--seq-5:#256abf;
--seq-6:#184f95;--seq-7:#0d366b;--ink-1:#0b0b0b;--ink-2:#0b0b0b;--ink-3:#0b0b0b;--ink-4:#fff;--ink-5:#fff;
--ink-6:#fff;--ink-7:#fff}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{--bar-neg:#e66767;
--band-outer:rgba(57,135,229,.18);--band-inner:rgba(57,135,229,.38);--warning:#fab219;--seq-1:#0d366b;
--seq-2:#184f95;--seq-3:#256abf;--seq-4:#3987e5;--seq-5:#6da7ec;--seq-6:#9ec5f4;--seq-7:#cde2fb;--ink-1:#fff;
--ink-2:#fff;--ink-3:#fff;--ink-4:#fff;--ink-5:#0b0b0b;--ink-6:#0b0b0b;--ink-7:#0b0b0b}}
:root[data-theme="dark"] .viz-root{--bar-neg:#e66767;--band-outer:rgba(57,135,229,.18);
--band-inner:rgba(57,135,229,.38);--warning:#fab219;--seq-1:#0d366b;--seq-2:#184f95;--seq-3:#256abf;
--seq-4:#3987e5;--seq-5:#6da7ec;--seq-6:#9ec5f4;--seq-7:#cde2fb;--ink-1:#fff;--ink-2:#fff;--ink-3:#fff;
--ink-4:#fff;--ink-5:#0b0b0b;--ink-6:#0b0b0b;--ink-7:#0b0b0b}
.verdicts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:16px}
.vd{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.vd h3{margin:0 0 6px;font-size:14px;display:flex;gap:8px;align-items:center}
.vd p{margin:0;color:var(--text-secondary);font-size:13px}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;border-radius:999px;
padding:2px 8px;border:1px solid currentColor;white-space:nowrap}
.badge.ok{color:var(--good)}.badge.warn{color:var(--warning)}.badge.info{color:var(--series-1)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.two>.card{margin-bottom:0}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--text-secondary);margin:6px 0 2px}
.sw{display:inline-block;width:14px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.heat td.c{text-align:center;border-radius:6px;border:2px solid var(--surface-1);padding:8px 6px;min-width:84px}
.heat td.c b{display:block;font-size:15px}.heat td.c span{font-size:11px;opacity:.85}
.heat td.cur{outline:2px solid var(--text-primary);outline-offset:-4px}
.heat th{text-align:center}
.scale{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);margin-top:8px}
.scale i{display:inline-block;width:22px;height:10px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}
.kv div:nth-child(odd){color:var(--text-secondary)}
ol.refs{font-size:12px;color:var(--text-secondary);padding-left:18px}ol.refs li{margin-bottom:4px}
details summary{cursor:pointer;color:var(--text-secondary);font-size:13px}
table.txt th,table.txt td{text-align:left;white-space:normal;vertical-align:top}
table.txt td.n{white-space:nowrap}
"""

JS = """
(function(){
  function place(tip, box, px, py){
    tip.style.display='block';
    var w=tip.offsetWidth, h=tip.offsetHeight, bw=box.clientWidth;
    tip.style.left=Math.max(0, Math.min(px+12, bw-w))+'px';
    tip.style.top=Math.max(0, py-h-8)+'px';
  }
  document.querySelectorAll('.vchart').forEach(function(c){
    var svg=c.querySelector('svg'), tip=c.querySelector('.tip'), W=+c.dataset.w, hl=svg.querySelector('.hl');
    var pts=c.dataset.points?JSON.parse(c.dataset.points):null;
    svg.addEventListener('mousemove',function(ev){
      var r=svg.getBoundingClientRect(), px=ev.clientX-r.left, py=ev.clientY-r.top;
      if(pts){
        var x=px*W/r.width, best=0, bd=1e9;
        for(var i=0;i<pts.length;i++){var d=Math.abs(pts[i][0]-x); if(d<bd){bd=d;best=i;}}
        hl.setAttribute('x1',pts[best][0]); hl.setAttribute('x2',pts[best][0]); hl.style.display='';
        tip.innerHTML=pts[best][1]; place(tip,c,pts[best][0]*r.width/W,py);
        return;
      }
      var t=ev.target.closest('[data-tip]');
      if(t){tip.innerHTML=t.getAttribute('data-tip'); place(tip,c,px,py);} else tip.style.display='none';
    });
    svg.addEventListener('mouseleave',function(){tip.style.display='none'; if(hl) hl.style.display='none';});
  });
})();
"""


def _e(x) -> str:
    return html.escape(str(x))


def _pct(x: float, d: int = 1, signed: bool = True) -> str:
    return f"{x:+.{d}f}%" if signed else f"{x:.{d}f}%"


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step) * step
    out, v = [], start
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def _bar_path(x: float, w: float, y0: float, y1: float, r: float = 4) -> str:
    """Bar from baseline y0 to data end y1 with the data end rounded (r<=w/2, r<=height)."""
    h = abs(y1 - y0)
    r = max(0.0, min(r, w / 2, h))
    if y1 <= y0:  # grows upward
        return (f"M{x:.1f},{y0:.1f} L{x:.1f},{y1 + r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} "
                f"L{x + w - r:.1f},{y1:.1f} Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 + r:.1f} L{x + w:.1f},{y0:.1f} Z")
    return (f"M{x:.1f},{y0:.1f} L{x:.1f},{y1 - r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} "
            f"L{x + w - r:.1f},{y1:.1f} Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 - r:.1f} L{x + w:.1f},{y0:.1f} Z")


def _fan(mc: dict) -> dict[int, list[float]]:
    return {int(k): v for k, v in mc["fan"].items()}


def fan_bounds(*mcs) -> tuple[float, float]:
    lo, hi = 0.0, 0.0
    for mc in mcs:
        if mc:
            f = _fan(mc)
            lo = min(lo, min(f[5]) - 1)
            hi = max(hi, max(f[95]) - 1)
    pad = (hi - lo) * 0.06
    return (lo - pad) * 100, (hi + pad) * 100


def fan_svg(mc: dict, ylo: float, yhi: float, live: list[dict] | None = None, w: int = 520, h: int = 260) -> str:
    f = _fan(mc)
    n = len(f[50])
    days = [0] + [7 * k + 1 for k in range(n)]
    ser = {q: [0.0] + [(x - 1) * 100 for x in f[q]] for q in f}
    pl, pr, pt, pb = 46, 12, 10, 26
    X = lambda d: pl + d / 365 * (w - pl - pr)
    Y = lambda y: pt + (yhi - y) / (yhi - ylo) * (h - pt - pb)
    parts = []
    for t in _ticks(ylo, yhi, 5):
        parts.append(f"<line x1='{pl}' x2='{w - pr}' y1='{Y(t):.1f}' y2='{Y(t):.1f}' stroke='var(--grid)'/>"
                     f"<text x='{pl - 6}' y='{Y(t) + 4:.1f}' text-anchor='end' fill='var(--muted)' "
                     f"font-size='11'>{t:+.0f}%</text>")
    for d, lab in ((0, "bugün"), (91, "3 ay"), (182, "6 ay"), (274, "9 ay"), (365, "12 ay")):
        anchor = "start" if d == 0 else ("end" if d == 365 else "middle")
        parts.append(f"<text x='{X(d):.1f}' y='{h - 8}' text-anchor='{anchor}' fill='var(--muted)' "
                     f"font-size='11'>{lab}</text>")

    def band(a, b, fill):
        up = " L".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in zip(days, ser[b]))
        dn = " L".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in zip(days[::-1], ser[a][::-1]))
        return f"<path d='M{up} L{dn} Z' fill='{fill}' stroke='none'/>"

    parts.append(band(5, 95, "var(--band-outer)"))
    parts.append(band(25, 75, "var(--band-inner)"))
    parts.append(f"<line x1='{pl}' x2='{w - pr}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' stroke='var(--text-secondary)' "
                 f"stroke-dasharray='3 4'/>")
    med = " L".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in zip(days, ser[50]))
    parts.append(f"<path d='M{med}' fill='none' stroke='var(--series-1)' stroke-width='2' stroke-linejoin='round'/>")
    for row in live or []:
        d = min(row["days"], 365)
        parts.append(f"<circle cx='{X(d):.1f}' cy='{Y(row['return'] * 100):.1f}' r='5' fill='var(--text-primary)' "
                     f"stroke='var(--surface-1)' stroke-width='2'/><text x='{X(d) + 8:.1f}' "
                     f"y='{Y(row['return'] * 100) - 8:.1f}' fill='var(--text-primary)' font-size='11'>"
                     f"{_e(row['mode'])} (sen)</text>")
    pts = []
    for i, d in enumerate(days):
        if i % 2 and i != len(days) - 1:
            continue
        tip = (f"<b>{d}. gün</b><br>ortanca {ser[50][i]:+.1f}%<br>%25-75: {ser[25][i]:+.1f} … {ser[75][i]:+.1f}%"
               f"<br>%5-95: {ser[5][i]:+.1f} … {ser[95][i]:+.1f}%")
        pts.append([round(X(d), 1), tip])
    return (f"<div class='chart vchart' data-w='{w}' data-points='{_e(json.dumps(pts))}'>"
            f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='12 aylık olası getiri yelpazesi'>{''.join(parts)}"
            f"<line class='hl' y1='{pt}' y2='{h - pb}' stroke='var(--muted)' style='display:none'/>"
            f"<rect x='0' y='0' width='{w}' height='{h}' fill='transparent'/></svg><div class='tip'></div></div>")


def hist_svg(mc: dict, xlo: float, xhi: float, w: int = 520, h: int = 200) -> str:
    counts, edges = mc["hist"], [e * 100 for e in mc["hist_edges"]]
    total = sum(counts) or 1
    pl, pr, pt, pb = 12, 12, 26, 26
    X = lambda v: pl + (v - xlo) / (xhi - xlo) * (w - pl - pr)
    cmax = max(counts) or 1
    tail = sum(c for c, a in zip(counts, edges[:-1]) if a >= xhi) / total
    Y = lambda c: pt + (1 - c / cmax) * (h - pt - pb)
    base = h - pb
    parts = [f"<line x1='{pl}' x2='{w - pr}' y1='{base}' y2='{base}' stroke='var(--grid)'/>"]
    for t in _ticks(xlo, xhi, 6):
        parts.append(f"<text x='{X(t):.1f}' y='{h - 8}' text-anchor='middle' fill='var(--muted)' "
                     f"font-size='11'>{t:+.0f}%</text>")
    for c, a, b in zip(counts, edges[:-1], edges[1:]):
        if not c or a >= xhi:
            continue
        b = min(b, xhi)
        x0, x1 = X(a) + 1, X(b) - 1
        if x1 - x0 < 1:
            x0, x1 = X(a), X(a) + 1
        mid = (a + b) / 2
        fill = "var(--bar-neg)" if mid < 0 else "var(--series-1)"
        tip = f"{a:+.0f}% … {b:+.0f}%<br><b>{c / total * 100:.1f}%</b> olasılık"
        parts.append(f"<path d='{_bar_path(x0, x1 - x0, base, Y(c), 2)}' fill='{fill}' data-tip='{_e(tip)}'/>")
    if xlo < 0 < xhi:
        parts.append(f"<line x1='{X(0):.1f}' x2='{X(0):.1f}' y1='{pt - 6}' y2='{base}' stroke='var(--text-secondary)' "
                     f"stroke-dasharray='3 4'/><text x='{X(0) - 6:.1f}' y='12' text-anchor='end' "
                     f"fill='var(--text-secondary)' font-size='11'>← zarar</text><text x='{X(0) + 6:.1f}' "
                     f"y='12' fill='var(--text-secondary)' font-size='11'>kâr →</text>")
    if tail > 0.0005:
        parts.append(f"<text x='{w - pr}' y='12' text-anchor='end' fill='var(--muted)' font-size='11'>"
                     f"+{xhi:.0f}% üstü: %{tail * 100:.1f}</text>")
    return (f"<div class='chart vchart' data-w='{w}'><svg viewBox='0 0 {w} {h}' role='img' "
            f"aria-label='12 ay sonundaki getiri dağılımı'>{''.join(parts)}</svg><div class='tip'></div></div>")


def yearly_svg(yearly: dict, w: int = 1000, h: int = 230) -> str:
    items = list(yearly.items())
    vals = [v for _, v in items]
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.12, hi + span * 0.12
    pl, pr, pt, pb = 46, 12, 10, 26
    Y = lambda v: pt + (hi - v) / (hi - lo) * (h - pt - pb)
    slot = (w - pl - pr) / len(items)
    bw = min(64, slot * 0.6)
    parts = []
    for t in _ticks(lo, hi, 5):
        parts.append(f"<line x1='{pl}' x2='{w - pr}' y1='{Y(t):.1f}' y2='{Y(t):.1f}' stroke='var(--grid)'/>"
                     f"<text x='{pl - 6}' y='{Y(t) + 4:.1f}' text-anchor='end' fill='var(--muted)' "
                     f"font-size='11'>{t:+.0f}%</text>")
    for i, (yr, v) in enumerate(items):
        x = pl + slot * i + (slot - bw) / 2
        fill = "var(--bar-neg)" if v < 0 else "var(--series-1)"
        tip = f"<b>{_e(yr)}</b><br>{v:+.1f}%"
        parts.append(f"<path d='{_bar_path(x, bw, Y(0), Y(v))}' fill='{fill}' data-tip='{_e(tip)}'/>")
        ly = Y(v) - 6 if v >= 0 else Y(v) + 14
        parts.append(f"<text x='{x + bw / 2:.1f}' y='{ly:.1f}' text-anchor='middle' fill='var(--text-primary)' "
                     f"font-size='12' font-weight='600'>{v:+.1f}%</text>"
                     f"<text x='{x + bw / 2:.1f}' y='{h - 8}' text-anchor='middle' fill='var(--muted)' "
                     f"font-size='11'>{_e(yr)}</text>")
    parts.append(f"<line x1='{pl}' x2='{w - pr}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' stroke='var(--text-secondary)'/>")
    return (f"<div class='chart vchart' data-w='{w}'><svg viewBox='0 0 {w} {h}' role='img' "
            f"aria-label='Yıllık getiriler'>{''.join(parts)}</svg><div class='tip'></div></div>")


def heatmap(grid: list[dict], cur_ema: str, cur_atr: float) -> str:
    emas = list(dict.fromkeys(g["ema"] for g in grid))
    atrs = sorted({g["atr"] for g in grid})
    lo = min(g["sharpe"] for g in grid)
    hi = max(g["sharpe"] for g in grid)
    cell = {(g["ema"], g["atr"]): g for g in grid}
    head = "".join(f"<th>{a:g}×ATR stop</th>" for a in atrs)
    rows = []
    for e in emas:
        tds = []
        for a in atrs:
            g = cell.get((e, a))
            if not g:
                tds.append("<td></td>")
                continue
            step = 1 + int(round((g["sharpe"] - lo) / ((hi - lo) or 1) * 6))
            cur = " cur" if (e == cur_ema and abs(a - cur_atr) < 1e-9) else ""
            tip = (f"EMA {e} · {a:g}×ATR — Sharpe {g['sharpe']:.2f}, yıllık %{g['cagr']:.1f}, "
                   f"en kötü düşüş %{g['dd']:.1f}, saklı dönem Sharpe {g['holdout_sharpe']:.2f}")
            tds.append(f"<td class='c{cur}' title='{_e(tip)}' style='background:var(--seq-{step});"
                       f"color:var(--ink-{step})'><b>{g['sharpe']:.2f}</b>"
                       f"<span>saklı {g['holdout_sharpe']:.2f}</span></td>")
        label = f"EMA {e}" if e != "-" else "-"
        rows.append(f"<tr><th style='text-align:left'>{_e(label)}</th>{''.join(tds)}</tr>")
    scale = "".join(f"<i style='background:var(--seq-{i})'></i>" for i in range(1, 8))
    return (f"<div class='scroll'><table class='heat'><thead><tr><th></th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            f"<div class='scale'>Sharpe {lo:.2f} {scale} {hi:.2f} · çerçeveli hücre: şu anki ayar · "
            f"küçük sayı: saklı dönem (2025→) Sharpe</div>")


def _table(head: list[str], rows: list[list[str]], cls: str = "") -> str:
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='scroll'><table class='{cls}'><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"


def _cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def _mc_tiles(mc: dict, title: str) -> str:
    tiles = [("Ortanca (12 ay)", _pct(mc["median"] * 100), _cls(mc["median"])),
             ("Kötü senaryo (%5)", _pct(mc["p5"] * 100), _cls(mc["p5"])),
             ("İyi senaryo (%95)", _pct(mc["p95"] * 100), _cls(mc["p95"])),
             ("Zararla kapatma olasılığı", f"%{mc['prob_loss'] * 100:.0f}", ""),
             ("%20+ düşüş olasılığı", f"%{mc['prob_dd_gt_20'] * 100:.0f}", ""),
             ("Tipik en kötü düşüş", _pct(mc["median_max_dd"] * 100), "neg")]
    t = "".join(f"<div class='tile'><div class='k'>{_e(k)}</div><div class='v {c}'>{_e(v)}</div></div>"
                for k, v, c in tiles)
    return f"<div class='note' style='margin:0 0 8px'><b>{_e(title)}</b></div><div class='tiles'>{t}</div>"


BADGE = {"ok": "✓ Geçti", "warn": "! Dikkat", "info": "i Bilgi"}


def render(r: dict) -> str:
    s = r["settings"]
    mc_a, mc_h = r["mc_all"], r.get("mc_holdout")
    live = r.get("live") or []

    vd = "".join(f"<div class='vd'><h3><span class='badge {v['state']}'>{BADGE[v['state']]}</span>"
                 f"{_e(v['title'])}</h3><p>{_e(v['text'])}</p></div>" for v in r["verdict"])

    prow = []
    for k in ("train", "holdout", "all"):
        m = r["periods"][k]
        prow.append([_e(PERIOD_NAMES[k]), f"<span class='{_cls(m['cagr_%'])}'>{m['cagr_%']:+.1f}%</span>",
                     f"{m['max_drawdown_%']:.1f}%", f"{m['sharpe']:.2f}", f"{m['sortino']:.2f}", str(m["trades"]),
                     f"%{m['win_rate_%']:.0f}", f"{m['profit_factor']:.2f}", f"{m['avg_win_loss_ratio']:.1f}"])
    periods = _table(["Dönem", "Yıllık getiri", "En kötü düşüş", "Sharpe", "Sortino", "İşlem", "İsabet",
                      "Kâr faktörü", "Ort. kazanç / kayıp"], prow)

    ylo, yhi = fan_bounds(mc_a, mc_h)
    mcs = [m for m in (mc_a, mc_h) if m]
    xlo = min([m["hist_edges"][0] * 100 for m in mcs] + [0])
    xhi = max(max(m["p95"] for m in mcs) * 100 * 1.35, 10)
    xhi = min(xhi, max(m["hist_edges"][-1] * 100 for m in mcs))
    fans = []
    for mc, title, desc in ((mc_h, "Kötümser: son 2 yıl tekrar ederse", "2025-26 günlük getirilerinden"),
                            (mc_a, "Tüm geçmiş tekrar ederse", "2021-26 günlük getirilerinden")):
        if not mc:
            continue
        fans.append(f"<div class='card'>{_mc_tiles(mc, title)}"
                    f"<div class='legend'><span><i class='sw' style='background:var(--series-1)'></i>ortanca yol</span>"
                    f"<span><i class='sw' style='background:var(--band-inner)'></i>%25-75 (iki senaryodan biri)</span>"
                    f"<span><i class='sw' style='background:var(--band-outer)'></i>%5-95 (10 senaryodan 9'u)</span>"
                    + ("<span>● senin botun</span>" if live and mc is (mc_h or mc_a) else "") +
                    f"</div>{fan_svg(mc, ylo, yhi, live if mc is (mc_h or mc_a) else None)}"
                    f"<div class='note' style='margin:10px 0 4px'>12 ay sonunda nerede olursun? ({_e(desc)}, "
                    f"5.000 simülasyon)</div>{hist_svg(mc, xlo, xhi)}</div>")

    live_html = ""
    if live:
        rows = [[f"<span class='badge {x['state']}'>{BADGE[x['state']]}</span> {_e(x['mode'].upper())}",
                 _e(x["start"]), f"{x['days']:.0f}", f"<span class='{_cls(x['return'])}'>{x['return'] * 100:+.2f}%</span>",
                 f"{x['band'][5] * 100:+.1f} … {x['band'][95] * 100:+.1f}%", f"{x['max_dd'] * 100:.1f}%",
                 _e(x["text"])] for x in live]
        live_html = (f"<h2>Gerçek takip: botun sonuçları beklentiyle uyumlu mu?</h2><div class='card'>"
                     f"<p class='note' style='margin-top:0'>En dürüst test budur: strateji seçilirken var olmayan veri. "
                     f"Her çalıştırmada güncellenir.</p>"
                     f"{_table(['Hesap', 'Başlangıç', 'Gün', 'Getiri', 'Beklenen aralık (%5-95)', 'En kötü düşüş', 'Durum'], rows, 'txt')}"
                     f"</div>")

    risk_html = ""
    if r.get("risk_curve"):
        names = {0.0025: "conservative", 0.005: "balanced", 0.0075: "aggressive"}
        rows = []
        for c in r["risk_curve"]:
            cur = abs(c["risk"] - s["risk_per_trade"]) < 1e-9
            f = (lambda x, d=0: "-" if x is None else f"%{x * 100:.{d}f}")
            cells = [f"%{c['risk'] * 100:.2f}" + (" <b>← şu an</b>" if cur else ""), names.get(c["risk"], ""),
                     f"<span class='{_cls(c['cagr'])}'>{c['cagr']:+.1f}%</span>", f"{c['dd']:.1f}%",
                     f"{c['sharpe']:.2f}", f"<span class='{_cls(c['holdout_cagr'])}'>{c['holdout_cagr']:+.1f}%</span>",
                     f"{c['holdout_dd']:.1f}%", f(c["prob_loss_12m"]), f(c["prob_dd_gt_20"]),
                     "-" if c["p5_12m"] is None else f"{c['p5_12m'] * 100:+.1f}%"]
            rows.append([f"<b>{x}</b>" if cur and i else x for i, x in enumerate(cells)])
        risk_html = (f"<h2>Risk seviyesi: daha fazla risk = daha fazla kazanç mı?</h2><div class='card'>"
                     f"{_table(['İşlem başı risk', 'Profil', 'Yıllık (tümü)', 'En kötü düşüş', 'Sharpe', 'Yıllık (saklı)', 'Düşüş (saklı)', '12 ay zarar olas.', '%20+ düşüş olas.', '12 ay kötü senaryo'], rows)}"
                     f"<p class='note'>Son üç sütun kötümser senaryodan (son 2 yıl tekrar ederse). Risk arttıkça getiri "
                     f"artar ama düşüş daha hızlı büyür. "
                     + ("Spot'ta pozisyonlar sermayeyi aşamadığı için bir noktadan sonra getiri artmaz, sadece risk artar. "
                        if s["market"] == "spot" else
                        "Futures'ta kaldıraçla getiri artmaya devam eder ama derin düşüş olasılığı çok hızlı büyür. ")
                     + "Profesyoneller genelde 'kaldırabileceğim en kötü düşüş'ten geriye doğru seçer.</p></div>")

    st = r.get("streaks") or {}
    pbo, dsr = r["pbo"], r["dsr"]
    psr_txt = "%99.9+" if r["psr"] > 0.999 else f"%{r['psr'] * 100:.1f}"
    stats = (f"<div class='kv'>"
             f"<div>PSR (Sharpe &gt; 0, tüm dönem)</div><div>{psr_txt}</div>"
             f"<div>PSR (saklı dönem)</div><div>%{r['psr_holdout'] * 100:.0f}</div>"
             f"<div>Deflated Sharpe, N=200 bağımsız deneme</div><div>%{dsr['dsr'] * 100:.0f} "
             f"(şans eşiği: yıllık Sharpe {dsr['sr0_annual']:.2f})</div>"
             f"<div>Deflated Sharpe, etkin N≈20</div><div>%{dsr['dsr_effective'] * 100:.0f} "
             f"(şans eşiği: yıllık Sharpe {dsr['sr0_annual_effective']:.2f})</div>"
             f"<div>PBO (CSCV, {pbo['combinations']:,} kombinasyon)</div><div>%{pbo['pbo'] * 100:.0f}</div>"
             f"<div>Maliyet stresi (ücret×2, kayma×3)</div><div>yıllık {r['cost_stress']['cagr_%']:+.1f}%, "
             f"Sharpe {r['cost_stress']['sharpe']:.2f}, düşüş {r['cost_stress']['max_drawdown_%']:.1f}%</div>"
             f"<div>Pozitif ay oranı</div><div>%{r['monthly']['positive_share'] * 100:.0f} "
             f"(en iyi {r['monthly']['best']:+.1f}%, en kötü {r['monthly']['worst']:+.1f}%)</div>"
             + (f"<div>Ard arda kayıp (100 işlemde)</div><div>tipik {st['median_worst_streak_per_100']:.0f}, "
                f"%95 ihtimalle ≤ {st['p95_worst_streak_per_100']:.0f}; geçmişte en uzun {st['historical_worst_streak']}"
                f"</div>" if st else "") + "</div>")

    coin_rows = [[_e(k), f"<span class='{_cls(v['pnl'])}'>{v['pnl']:+,.0f}</span>", str(v["trades"]),
                  f"%{v['win_rate'] * 100:.0f}"]
                 for k, v in sorted((r.get("per_coin") or {}).items(), key=lambda kv: -kv[1]["pnl"])]
    reg = r.get("regime") or {}
    reg_rows = [[{"btc_up": "BTC yükselişte (EMA200 üstü)", "btc_down": "BTC düşüşte"}.get(k, k),
                 f"<span class='{_cls(v['pnl'])}'>{v['pnl']:+,.0f}</span>", str(v["trades"])] for k, v in reg.items()]

    lit = _table(["Fikir", "Kaynak", "", "Sonuç (eğitim + saklı dönem)"],
                 [[_e(a), _e(b), c, _e(d)] for a, b, c, d in LITERATURE], "txt")
    refs = "".join(f"<li>{x}</li>" for x in REFERENCES)
    syms = ", ".join(x.split("/")[0] for x in s["symbols"])

    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Strateji Doğrulama</title>
<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">
<h1>Strateji doğrulama raporu</h1>
<p class="sub">{_e(s['market'])} · {_e(s['strategy'])} · profil {_e(s['profile'])} · işlem başı risk
%{s['risk_per_trade'] * 100:.2f} · stop {s['atr_stop_mult']:g}×ATR · en fazla {s['max_open_positions']} pozisyon ·
BTC filtresi {'açık' if s['btc_filter'] else 'kapalı'} · {_e(syms)} · oluşturuldu {_e(r['generated'][:16].replace('T', ' '))} UTC</p>

<div class="card"><b>Kısaca:</b> Bu rapor "ne kadar kazanırım" sorusuna değil, <b>"bu backtest'e ne kadar
güvenebilirim ve önümüzdeki 12 ayda makul aralık ne"</b> sorusuna cevap verir. Hiçbir test kâr garantisi
veremez; buradaki olasılıklar geçmiş verinin tekrar etmesi varsayımına dayanır.</div>

<h2>Karar özeti</h2><div class="verdicts">{vd}</div>
{live_html}
<h2>Geçmiş performans</h2><div class="card">{periods}
<p class="note">Eğitim döneminde ayarlar seçildi; saklı dönem (2025→) bu seçimde kullanılmadı. İkisi arasındaki
büyük fark normal: geçmişteki en iyi dönem hiçbir zaman beklenti değildir.</p></div>
<div class="card"><div class="note" style="margin-bottom:6px">Yıllık getiri (2026: yıl başından bugüne)</div>
{yearly_svg(r['yearly'])}</div>

<h2>İleriye dönük: önümüzdeki 12 ay</h2>
<p class="note">Durağan blok bootstrap (Politis-Romano): geçmişteki günlük getiriler, ardışık bloklar halinde
(ortalama 20 gün) karıştırılarak 5.000 farklı olası yıl üretildi. Soldaki kötümser senaryo son 2 yılın daha zayıf
piyasasını esas alır; plan yaparken onu kullan.</p>
<div class="two">{''.join(fans)}</div>

{risk_html}
<h2>Sağlamlık testleri</h2>
<div class="two"><div class="card"><b>İstatistikler</b><div style="margin-top:10px">{stats}</div>
<details style="margin-top:12px"><summary>Bu sayılar ne demek?</summary><p class="note">
<b>PSR</b>: getirilerin çarpıklığı ve kalın kuyrukları hesaba katılarak gerçek Sharpe'ın sıfırdan büyük olma
olasılığı. <b>Deflated Sharpe</b>: aynı şey, ama çok sayıda varyant denendiği için "en iyisinin şans eseri
çıkması" düzeltilerek. %95 üstü çok güçlü, %50 civarı "şanstan ayırt etmek zor" demek. <b>PBO</b>: komşu
ayarlar arasından geçmişte en iyisini seçmenin gelecekte ortalamanın altında kalma olasılığı; %50 civarı
"ince ayarın faydası yok" demek, bu yüzden bot sabit ve sade bir ayar kullanır.</p></details></div>
<div class="card"><b>Parametre haritası (tüm dönem Sharpe)</b><p class="note">Komşu ayarlar da benzer sonuç
veriyorsa sonuç şansa değil fikre dayanıyordur.</p>{heatmap(r['grid'], '20/50' if s['strategy'] == 'ema_trend' else '-', s['atr_stop_mult'])}</div></div>

<div class="two" style="margin-top:16px"><div class="card"><b>Coin bazında katkı (USDT, 1.000 başlangıç)</b>
{_table(['Coin', 'PnL', 'İşlem', 'İsabet'], coin_rows)}</div>
<div class="card"><b>Piyasa rejimine göre</b>{_table(['Rejim', 'PnL', 'İşlem'], reg_rows)}
<p class="note">Trend stratejisi kazancının çoğunu güçlü trendlerde yapar; yatay piyasada küçük stop'larla
para kaybeder. Bu normaldir ve uzun kayıp serilerinin sebebidir.</p></div></div>

<h2>Literatür ve denenen fikirler</h2><div class="card">{lit}
<p class="note">Bir fikir sadece hem 2021-24'te hem de hiç kullanılmamış 2025-26'da mevcut ayarı geçerse bota
alınır. Bu kural tek başına en büyük korumadır: çoğu "harika" backtest bu ikinci testte çöker.</p></div>

<h2>Yöntem ve kaynaklar</h2><div class="card"><p class="note" style="margin-top:0">Saklı dönem tamamen temiz
değil: stop mesafesi ilk araştırmada tüm veriyle seçildi ve BTC filtresi bu dönemde de kontrol edildi. Bu
yüzden saklı dönem sonuçlarını hafif iyimser say. Gerçekten temiz tek test botun bundan sonraki paper/demo
sonuçlarıdır (yukarıdaki "Gerçek takip").</p><ol class="refs">{refs}</ol></div>

<p class="note" style="margin-top:24px">Geçmiş sonuçlar geleceği garanti etmez. Bu bir yatırım tavsiyesi değildir.</p>
</div></div><script>{JS}</script></body></html>"""
