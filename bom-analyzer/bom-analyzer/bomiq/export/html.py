"""
Self-contained HTML report.

One file, no external requests, prints cleanly and opens on any device. Useful
for e-mailing a BOM review to someone who does not have the app, and as the
print path for design reviews.

The charts are hand-drawn inline SVG (no chart library), so the file stays
small and renders identically everywhere including in e-mail clients that
strip scripts. Sorting and filtering degrade gracefully: with JavaScript
enabled the tables are interactive, without it they are still complete.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from ..analysis.health import COMPONENT_LABELS, top_risks
from ..core.errors import ExportError
from ..core.models import (
    BomAnalysis, ComplianceState, Lifecycle, LineResult, RiskLevel, Severity,
)
from ..util import log
from ..util.text import truncate
from ..util.units import format_lead_time
from ..version import APP_TITLE, __version__

LOG = log.get("export.html")

RISK_COLOR = {
    RiskLevel.LOW: "#16a34a",
    RiskLevel.MEDIUM: "#d97706",
    RiskLevel.HIGH: "#ea580c",
    RiskLevel.CRITICAL: "#dc2626",
}

LIFECYCLE_COLOR = {
    Lifecycle.ACTIVE: "#16a34a",
    Lifecycle.NEW: "#2563eb",
    Lifecycle.UNKNOWN: "#6b7280",
    Lifecycle.NRND: "#d97706",
    Lifecycle.EOL: "#ea580c",
    Lifecycle.OBSOLETE: "#dc2626",
}

SEVERITY_COLOR = {
    Severity.ERROR: "#dc2626",
    Severity.WARNING: "#d97706",
    Severity.INFO: "#6b7280",
}

COMPLIANCE_COLOR = {
    ComplianceState.COMPLIANT: "#16a34a",
    ComplianceState.EXEMPT: "#d97706",
    ComplianceState.NON_COMPLIANT: "#dc2626",
    ComplianceState.UNKNOWN: "#6b7280",
}

_CSS = """
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#ffffff;
--panel:#f8fafc;--accent:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1360px;margin:0 auto;padding:28px 24px 72px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:38px 0 12px;padding-bottom:7px;
border-bottom:1px solid var(--line);letter-spacing:-.01em}
h3{font-size:14px;margin:22px 0 8px;color:var(--muted);
text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:22px}
.banner{padding:11px 14px;border-radius:8px;margin:0 0 20px;font-size:13px;
border:1px solid}
.banner.warn{background:#fffbeb;border-color:#fcd34d;color:#78350f}
.banner.info{background:#eff6ff;border-color:#bfdbfe;color:#1e3a8a}
.hero{display:flex;gap:22px;align-items:stretch;flex-wrap:wrap;
margin-bottom:8px}
.gauge{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:18px 22px;min-width:250px;display:flex;gap:18px;align-items:center}
.gauge .headline{font-weight:600;font-size:14px}
.gauge .grade{font-size:12px;color:var(--muted);margin-top:3px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));
gap:12px;flex:1}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:12px 14px}
.tile .k{font-size:11px;color:var(--muted);text-transform:uppercase;
letter-spacing:.05em}
.tile .v{font-size:21px;font-weight:600;margin-top:3px;letter-spacing:-.02em}
.tile .n{font-size:11.5px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
th{text-align:left;background:#1f2937;color:#fff;padding:8px 9px;
font-weight:600;font-size:11.5px;position:sticky;top:0;white-space:nowrap}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{background:#374151}
td{padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:nth-child(even){background:#fcfcfd}
tbody tr:hover{background:#f1f5f9}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
font-weight:600;color:#fff;white-space:nowrap}
.pill.soft{background:#eef2f7;color:#334155;font-weight:500}
.bar{height:7px;border-radius:4px;background:#e2e8f0;overflow:hidden;
min-width:64px}
.bar>span{display:block;height:100%}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);
margin:8px 0 0}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;
margin-right:5px}
details{margin:8px 0}
summary{cursor:pointer;font-weight:600;font-size:13px}
.controls{display:flex;gap:9px;align-items:center;margin:12px 0 4px;
flex-wrap:wrap}
.controls input,.controls select{padding:6px 9px;border:1px solid var(--line);
border-radius:7px;font-size:13px;font-family:inherit}
.controls input{min-width:230px}
.scroll{max-height:640px;overflow:auto;border:1px solid var(--line);
border-radius:9px}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);
color:var(--muted);font-size:11.5px}
.muted{color:var(--muted)}
.nowrap{white-space:nowrap}
@media print{
 body{font-size:10.5px}
 .controls,.scroll{max-height:none;overflow:visible;border:0}
 th{position:static}
 h2{page-break-after:avoid}
 tr{page-break-inside:avoid}
 .tile,.gauge{break-inside:avoid}
}
"""

_JS = """
(function(){
 function norm(v){var n=parseFloat(String(v).replace(/[^0-9.\\-]/g,''));
  return isNaN(n)?null:n;}
 document.querySelectorAll('table[data-sortable]').forEach(function(table){
  table.querySelectorAll('thead th').forEach(function(th,index){
   th.classList.add('sortable');
   th.addEventListener('click',function(){
    var body=table.tBodies[0];
    var rows=Array.prototype.slice.call(body.rows);
    var dir=th.dataset.dir==='asc'?-1:1;
    table.querySelectorAll('thead th').forEach(function(o){
     if(o!==th){delete o.dataset.dir;o.textContent=o.textContent.replace(
      /[ \\u25b2\\u25bc]+$/,'');}});
    th.dataset.dir=dir===1?'asc':'desc';
    rows.sort(function(a,b){
     var x=a.cells[index]?a.cells[index].innerText.trim():'';
     var y=b.cells[index]?b.cells[index].innerText.trim():'';
     var nx=norm(x),ny=norm(y);
     if(nx!==null&&ny!==null)return (nx-ny)*dir;
     return x.localeCompare(y)*dir;});
    rows.forEach(function(r){body.appendChild(r);});
    th.textContent=th.textContent.replace(/[ \\u25b2\\u25bc]+$/,'')+
     (dir===1?' \\u25b2':' \\u25bc');
   });
  });
 });
 document.querySelectorAll('[data-filter-for]').forEach(function(input){
  var table=document.getElementById(input.dataset.filterFor);
  if(!table)return;
  function apply(){
   var q=input.value.toLowerCase();
   var select=document.querySelector('[data-select-for="'+
    input.dataset.filterFor+'"]');
   var want=select?select.value:'';
   var shown=0;
   Array.prototype.forEach.call(table.tBodies[0].rows,function(row){
    var text=row.innerText.toLowerCase();
    var ok=(!q||text.indexOf(q)>=0)&&
     (!want||(row.dataset.tags||'').indexOf(want)>=0);
    row.style.display=ok?'':'none';if(ok)shown++;});
   var counter=document.querySelector('[data-count-for="'+
    input.dataset.filterFor+'"]');
   if(counter)counter.textContent=shown+' of '+
    table.tBodies[0].rows.length+' rows';
  }
  input.addEventListener('input',apply);
  var select=document.querySelector('[data-select-for="'+
   input.dataset.filterFor+'"]');
  if(select)select.addEventListener('change',apply);
  apply();
 });
})();
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any, places: int = 2) -> str:
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except Exception:  # pragma: no cover
        return _e(value)
    quantum = Decimal(1).scaleb(-places)
    return f"{number.quantize(quantum):,}"


def _pill(text: str, color: str) -> str:
    if not text:
        return ""
    return f'<span class="pill" style="background:{color}">{_e(text)}</span>'


def _bar(value: float, color: str, maximum: float = 100.0) -> str:
    share = max(0.0, min(100.0, (value / maximum) * 100.0 if maximum else 0.0))
    return (f'<div class="bar"><span style="width:{share:.1f}%;'
            f'background:{color}"></span></div>')


def _gauge_svg(score: float, color: str, size: int = 104) -> str:
    """Donut gauge as inline SVG."""
    radius = size / 2 - 9
    circumference = 2 * 3.14159265 * radius
    filled = circumference * max(0.0, min(100.0, score)) / 100.0
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'role="img" aria-label="Health score {score:.0f} of 100">'
        f'<circle cx="{size/2}" cy="{size/2}" r="{radius}" fill="none" '
        f'stroke="#e2e8f0" stroke-width="10"/>'
        f'<circle cx="{size/2}" cy="{size/2}" r="{radius}" fill="none" '
        f'stroke="{color}" stroke-width="10" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.2f} {circumference:.2f}" '
        f'transform="rotate(-90 {size/2} {size/2})"/>'
        f'<text x="50%" y="50%" text-anchor="middle" dy="0.36em" '
        f'font-size="25" font-weight="700" fill="#0f172a">'
        f'{score:.0f}</text></svg>'
    )


def _stacked_bar(items: Sequence[tuple[str, int, str]], total: int) -> str:
    """Horizontal stacked bar plus a text legend."""
    if not total:
        return '<p class="muted">No data.</p>'
    segments = []
    for label, count, color in items:
        if count <= 0:
            continue
        share = 100.0 * count / total
        segments.append(
            f'<span title="{_e(label)}: {count}" style="width:{share:.2f}%;'
            f'background:{color}"></span>')
    legend = " ".join(
        f'<span><i style="background:{color}"></i>{_e(label)} '
        f'<strong>{count}</strong></span>'
        for label, count, color in items if count > 0
    )
    return (f'<div class="bar" style="height:13px;border-radius:6px;'
            f'display:flex">{"".join(segments)}</div>'
            f'<div class="legend">{legend}</div>')


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def _header(analysis: BomAnalysis) -> str:
    bom = analysis.bom
    parts = [
        f'<h1>{_e(bom.name or "BOM analysis")}</h1>',
        f'<div class="sub">{_e(Path(bom.source_file).name or "uploaded file")}'
        f' &middot; {bom.line_count} lines'
        f' &middot; analysed {_e((analysis.finished_at or "").replace("T", " ").replace("Z", " UTC"))}'
        f' &middot; {APP_TITLE} v{_e(__version__)}</div>',
    ]
    if analysis.offline:
        parts.append(
            '<div class="banner warn"><strong>Offline mode.</strong> '
            'Pricing, stock and lifecycle figures below come from the '
            'built-in synthetic catalogue, not live distributor data. Add an '
            'API key to get real numbers.</div>')
    for warning in analysis.warnings:
        if analysis.offline and warning.startswith("Running on the offline"):
            continue
        parts.append(f'<div class="banner info">{_e(warning)}</div>')
    if bom.metadata:
        meta = " &middot; ".join(
            f"<strong>{_e(key)}:</strong> {_e(value)}"
            for key, value in list(bom.metadata.items())[:8])
        parts.append(f'<div class="sub">{meta}</div>')
    return "\n".join(parts)


def _hero(analysis: BomAnalysis) -> str:
    summary = analysis.summary
    health = analysis.health
    color = RISK_COLOR[health.level]
    tiles = [
        ("Lines", f"{summary.total_lines:,}",
         f"{summary.unique_parts:,} unique &middot; "
         f"{summary.placements:,} placements"),
        ("Matched", f"{summary.matched_lines:,}",
         f"{summary.unmatched_lines} unmatched &middot; "
         f"{summary.review_lines} to review"),
        (f"Total cost ({summary.currency})", _num(summary.total_cost),
         f"{summary.cost_coverage_pct:.0f}% of lines costed"),
        (f"Per assembly ({summary.currency})", _num(summary.cost_per_unit),
         f"build of {summary.build_quantity:,}"),
        ("Supply flags",
         f"{summary.out_of_stock_lines + summary.single_source_lines + summary.long_lead_lines:,}",
         f"{summary.out_of_stock_lines} no stock &middot; "
         f"{summary.single_source_lines} single source &middot; "
         f"{summary.long_lead_lines} long lead"),
        (f"Cheapest-source saving ({summary.currency})",
         _num(summary.potential_savings),
         "vs buying every line from its dearest source"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="k">{_e(k)}</div>'
        f'<div class="v">{v}</div><div class="n">{n}</div></div>'
        for k, v, n in tiles
    )
    drivers = ""
    if health.drivers:
        drivers = (f'<div class="n" style="margin-top:6px">Drivers: '
                   f'{_e("; ".join(health.drivers))}</div>')
    return (
        '<div class="hero">'
        f'<div class="gauge">{_gauge_svg(health.score, color)}'
        f'<div><div class="headline">{_e(health.headline)}</div>'
        f'<div class="grade">Grade <strong>{_e(health.grade)}</strong>'
        f' &middot; {_e(health.level.value)} risk</div>{drivers}</div></div>'
        f'<div class="tiles">{tile_html}</div>'
        '</div>'
    )


def _components(analysis: BomAnalysis) -> str:
    health = analysis.health
    if not health.components:
        return ""
    rows = []
    for key, value in sorted(health.components.items(), key=lambda kv: kv[1]):
        level = RiskLevel.from_score(100.0 - value)
        rows.append(
            f'<tr><td>{_e(COMPONENT_LABELS.get(key, key))}</td>'
            f'<td class="num">{value:.0f}</td>'
            f'<td style="width:180px">{_bar(value, RISK_COLOR[level])}</td>'
            f'<td class="num muted">{health.weights.get(key, 1.0):g}</td></tr>')
    return (
        '<h2>Health components</h2>'
        '<table><thead><tr><th>Component</th><th class="num">Score</th>'
        '<th></th><th class="num">Weight</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '<p class="muted" style="font-size:11.5px">Each component is 0-100 '
        'where higher is healthier, weighted by placement count so parts used '
        'many times on the board carry more influence.</p>'
    )


def _distributions(analysis: BomAnalysis) -> str:
    summary = analysis.summary
    total = summary.total_lines or 1
    lifecycle_items = []
    for status in (Lifecycle.ACTIVE, Lifecycle.NEW, Lifecycle.UNKNOWN,
                   Lifecycle.NRND, Lifecycle.EOL, Lifecycle.OBSOLETE):
        count = summary.lifecycle_counts.get(status.value, 0)
        lifecycle_items.append((status.value, count,
                                LIFECYCLE_COLOR[status]))
    no_data = summary.lifecycle_counts.get("No data", 0)
    if no_data:
        lifecycle_items.append(("No data", no_data, "#94a3b8"))

    risk_items = [
        (level.value, summary.risk_counts.get(level.value, 0),
         RISK_COLOR[level])
        for level in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH,
                      RiskLevel.CRITICAL)
    ]
    return (
        '<h2>Distributions</h2>'
        '<h3>Lifecycle status</h3>'
        f'{_stacked_bar(lifecycle_items, total)}'
        '<h3>Risk level</h3>'
        f'{_stacked_bar(risk_items, total)}'
    )


def _top_risks(analysis: BomAnalysis) -> str:
    entries = top_risks(analysis.results, limit=15)
    if not entries:
        return ('<h2>Top risks</h2><p class="muted">No lines carry '
                'measurable supply risk.</p>')
    rows = []
    for entry in entries:
        level = RiskLevel(str(entry["risk_level"]))
        rows.append(
            f'<tr><td class="num">{entry["line_no"]}</td>'
            f'<td class="mono">{_e(entry["mpn"])}</td>'
            f'<td>{_e(entry["manufacturer"])}</td>'
            f'<td class="num">{entry["quantity"]:g}</td>'
            f'<td class="num">{entry["risk_score"]:.0f}</td>'
            f'<td>{_pill(level.value, RISK_COLOR[level])}</td>'
            f'<td>{_e(entry["driver"])}</td>'
            f'<td>{_e(entry["detail"])}</td>'
            f'<td>{_e(entry["best_alternate"])}</td></tr>')
    return (
        '<h2>Top risks</h2>'
        '<table data-sortable><thead><tr><th class="num">Line</th><th>MPN</th>'
        '<th>Manufacturer</th><th class="num">Qty</th>'
        '<th class="num">Score</th><th>Level</th><th>Driver</th>'
        '<th>Why</th><th>Best alternate</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _soft_pills(values: Sequence[str]) -> str:
    return " ".join(
        '<span class="pill soft">' + _e(value) + '</span>' for value in values
    )


def _line_row(result: LineResult) -> str:
    line, part, cost = result.line, result.part, result.cost
    lifecycle = part.lifecycle if part else Lifecycle.UNKNOWN
    lead = min((o.lead_time_days for o in (part.offers if part else [])
                if o.lead_time_days is not None), default=None)
    tags = " ".join(tag for tag in [
        result.risk.level.value,
        lifecycle.value if part else "NoData",
        "DNP" if line.dnp else "",
        "Review" if result.match.needs_review else "",
        "NoStock" if part and not part.in_stock_offers else "",
    ] if tag)
    confidence_color = ("#16a34a" if result.match.confidence >= 90
                        else "#d97706" if result.match.confidence >= 60
                        else "#dc2626")
    mpn_cell = _e(line.mpn) or '<span class="muted">&mdash;</span>'
    if line.dnp:
        mpn_cell += ' <span class="pill soft">DNP</span>'
    lifecycle_cell = _pill(
        lifecycle.value if part else "No data",
        LIFECYCLE_COLOR.get(lifecycle, "#94a3b8") if part else "#94a3b8")
    stock_cell = (f'<td class="num">{part.total_stock:,}</td>' if part
                  else '<td class="num muted">&mdash;</td>')
    confidence = result.match.confidence or ""

    return (
        f'<tr data-tags="{_e(tags)}">'
        f'<td class="num">{line.line_no}</td>'
        f'<td class="mono">{mpn_cell}</td>'
        f'<td>{_e(line.manufacturer)}</td>'
        f'<td>{_e(truncate(line.description, 64))}</td>'
        f'<td class="num">{line.quantity:g}</td>'
        f'<td class="mono">{_e(truncate(line.ref_text, 28))}</td>'
        f'<td>{lifecycle_cell}</td>'
        f'<td class="num" style="color:{confidence_color};font-weight:600">'
        f'{confidence}</td>'
        f'{stock_cell}'
        f'<td class="num nowrap">{_e(format_lead_time(lead))}</td>'
        f'<td class="num">{_num(cost.unit_price, 5)}</td>'
        f'<td class="num">{_num(cost.extended)}</td>'
        f'<td>{_e(cost.best.distributor if cost.best else "")}</td>'
        f'<td class="num">{result.risk.score:.0f}</td>'
        f'<td>{_pill(result.risk.level.value, RISK_COLOR[result.risk.level])}</td>'
        f'<td>{_soft_pills(result.risk.flags)}</td>'
        f'</tr>'
    )


def _line_table(analysis: BomAnalysis) -> str:
    currency = analysis.summary.currency
    rows = [_line_row(result) for result in analysis.results]
    return (
        f'<h2>Enriched BOM</h2>'
        f'<div class="controls">'
        f'<input type="search" placeholder="Filter lines…" '
        f'data-filter-for="lines">'
        f'<select data-select-for="lines">'
        f'<option value="">All lines</option>'
        f'<option value="Critical">Critical risk</option>'
        f'<option value="High">High risk</option>'
        f'<option value="Review">Needs review</option>'
        f'<option value="NoStock">No stock</option>'
        f'<option value="Obsolete">Obsolete</option>'
        f'<option value="EOL">End of life</option>'
        f'<option value="NRND">NRND</option>'
        f'<option value="DNP">Do not populate</option>'
        f'</select>'
        f'<span class="muted" data-count-for="lines"></span></div>'
        f'<div class="scroll"><table id="lines" data-sortable><thead><tr>'
        f'<th class="num">#</th><th>MPN</th><th>Manufacturer</th>'
        f'<th>Description</th><th class="num">Qty</th><th>Refs</th>'
        f'<th>Lifecycle</th><th class="num">Conf</th><th class="num">Stock</th>'
        f'<th class="num">Lead</th><th class="num">Unit ({_e(currency)})</th>'
        f'<th class="num">Ext ({_e(currency)})</th><th>Source</th>'
        f'<th class="num">Risk</th><th>Level</th><th>Flags</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _alternates(analysis: BomAnalysis) -> str:
    rows = []
    for result in analysis.results:
        for alternate in result.alternates:
            drop_in = ("Yes" if alternate.pin_compatible else
                       "No" if alternate.pin_compatible is False else "Check")
            color = ("#16a34a" if alternate.score >= 75 else
                     "#d97706" if alternate.score >= 55 else "#dc2626")
            rows.append(
                f'<tr><td class="num">{result.line.line_no}</td>'
                f'<td class="mono">{_e(result.line.mpn)}</td>'
                f'<td class="mono"><strong>{_e(alternate.mpn)}</strong></td>'
                f'<td>{_e(alternate.manufacturer)}</td>'
                f'<td class="num" style="color:{color};font-weight:600">'
                f'{alternate.score}</td>'
                f'<td>{_e(drop_in)}</td>'
                f'<td>{_pill(alternate.lifecycle.value, LIFECYCLE_COLOR.get(alternate.lifecycle, "#94a3b8"))}</td>'
                f'<td class="num">{alternate.stock or ""}</td>'
                f'<td class="num">{_num(alternate.unit_price, 5)}</td>'
                f'<td class="num">'
                f'{f"{alternate.price_delta_pct:+.0f}%" if alternate.price_delta_pct is not None else ""}</td>'
                f'<td>{_e("; ".join(alternate.reasons))}</td>'
                f'<td>{_e("; ".join(alternate.concerns))}</td></tr>')
    if not rows:
        return ('<h2>Alternates</h2><p class="muted">No alternates were '
                'needed: every populated line is active, stocked and '
                'multi-sourced.</p>')
    return (
        '<h2>Alternates</h2>'
        '<div class="scroll"><table data-sortable><thead><tr>'
        '<th class="num">Line</th><th>Original</th><th>Alternate</th>'
        '<th>Manufacturer</th><th class="num">Score</th><th>Drop-in</th>'
        '<th>Lifecycle</th><th class="num">Stock</th><th class="num">Unit</th>'
        '<th class="num">&Delta;</th><th>Why it fits</th><th>Concerns</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _compliance(analysis: BomAnalysis) -> str:
    rows = []
    for result in analysis.results:
        compliance = result.compliance
        if compliance.rohs is ComplianceState.COMPLIANT and \
                compliance.reach in (ComplianceState.COMPLIANT,
                                     ComplianceState.UNKNOWN) and \
                not compliance.export_controlled and not compliance.itar:
            continue
        rows.append(
            f'<tr><td class="num">{result.line.line_no}</td>'
            f'<td class="mono">{_e(result.line.mpn)}</td>'
            f'<td>{_pill(compliance.rohs.value, COMPLIANCE_COLOR[compliance.rohs])}</td>'
            f'<td>{_e(compliance.rohs_note)}</td>'
            f'<td>{_pill(compliance.reach.value, COMPLIANCE_COLOR[compliance.reach])}</td>'
            f'<td>{_e("; ".join(compliance.svhc))}</td>'
            f'<td>{_e(compliance.country_of_origin)}</td>'
            f'<td class="mono">{_e(compliance.hts_code)}</td>'
            f'<td class="mono">{_e(compliance.eccn)}</td>'
            f'<td>{"ITAR" if compliance.itar else ("Yes" if compliance.export_controlled else "")}</td>'
            f'</tr>')
    rules = analysis.summary.rules or {}
    rule_text = (f'RoHS required: <strong>'
                 f'{"yes" if rules.get("require_rohs", True) else "no"}</strong>'
                 f' &middot; REACH required: <strong>'
                 f'{"yes" if rules.get("require_reach") else "no"}</strong>'
                 f' &middot; export control flagged: <strong>'
                 f'{"yes" if rules.get("flag_export_controlled", True) else "no"}'
                 f'</strong>')
    if not rows:
        return (f'<h2>Compliance</h2><p class="muted">No compliance '
                f'exceptions were found. {rule_text}</p>')
    return (
        '<h2>Compliance exceptions</h2>'
        f'<p class="muted" style="font-size:11.5px">{rule_text}</p>'
        '<table data-sortable><thead><tr><th class="num">Line</th><th>MPN</th>'
        '<th>RoHS</th><th>Note</th><th>REACH</th><th>SVHC</th><th>Origin</th>'
        '<th>HTS</th><th>ECCN</th><th>Export</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '<p class="muted" style="font-size:11.5px">Only lines with an '
        'exception are listed. &ldquo;Unknown&rdquo; means no declaration was '
        'found, not that the part is compliant.</p>'
    )


def _issues(analysis: BomAnalysis) -> str:
    entries: list[tuple[Severity, str, str, str, str]] = []
    for issue in analysis.issues:
        entries.append((issue.severity, "BOM", "", issue.message,
                        issue.suggestion or ""))
    for result in analysis.results:
        for issue in result.all_issues:
            entries.append((issue.severity, str(result.line.line_no),
                            result.line.mpn, issue.message,
                            issue.suggestion or ""))
    entries.sort(key=lambda item: -item[0].rank)
    if not entries:
        return '<h2>Findings</h2><p class="muted">No findings.</p>'
    rows = "".join(
        f'<tr data-tags="{severity.value}">'
        f'<td>{_pill(severity.value.upper(), SEVERITY_COLOR[severity])}</td>'
        f'<td class="num">{_e(scope)}</td><td class="mono">{_e(mpn)}</td>'
        f'<td>{_e(message)}</td><td class="muted">{_e(suggestion)}</td></tr>'
        for severity, scope, mpn, message, suggestion in entries
    )
    counts = {severity: sum(1 for e in entries if e[0] is severity)
              for severity in Severity}
    chips = " ".join(
        f'{_pill(f"{severity.value}: {count}", SEVERITY_COLOR[severity])}'
        for severity, count in counts.items() if count
    )
    return (
        f'<h2>Findings</h2><p>{chips}</p>'
        f'<div class="controls">'
        f'<input type="search" placeholder="Filter findings…" '
        f'data-filter-for="issues">'
        f'<select data-select-for="issues"><option value="">All</option>'
        f'<option value="error">Errors</option>'
        f'<option value="warning">Warnings</option>'
        f'<option value="info">Info</option></select>'
        f'<span class="muted" data-count-for="issues"></span></div>'
        f'<div class="scroll"><table id="issues"><thead><tr><th>Severity</th>'
        f'<th class="num">Line</th><th>MPN</th><th>Finding</th>'
        f'<th>Suggested action</th></tr></thead><tbody>{rows}</tbody></table>'
        f'</div>'
    )


def _providers(analysis: BomAnalysis) -> str:
    stats = analysis.summary.provider_stats or {}
    rows = []
    for provider_id, data in sorted(stats.items()):
        if provider_id.startswith("_"):
            continue
        note = (data.get("skipped") or data.get("disabled_reason")
                or data.get("last_error") or "")
        rows.append(
            f'<tr><td class="mono">{_e(provider_id)}</td>'
            f'<td>{_e(data.get("name", ""))}</td>'
            f'<td class="num">{data.get("calls", 0)}</td>'
            f'<td class="num">{data.get("hits", 0)}</td>'
            f'<td class="num">{data.get("cache_hits", 0)}</td>'
            f'<td class="num">{data.get("errors", 0)}</td>'
            f'<td class="num">{data.get("avg_latency_ms", 0)} ms</td>'
            f'<td class="muted">{_e(truncate(str(note), 160))}</td></tr>')
    if not rows:
        return ""
    return (
        '<h2>Data sources</h2>'
        '<table><thead><tr><th>Provider</th><th>Name</th>'
        '<th class="num">Calls</th><th class="num">Hits</th>'
        '<th class="num">Cached</th><th class="num">Errors</th>'
        '<th class="num">Latency</th><th>Notes</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #

def render(analysis: BomAnalysis) -> str:
    """Return the complete HTML document as a string."""
    body = "\n".join([
        _header(analysis),
        _hero(analysis),
        _components(analysis),
        _distributions(analysis),
        _top_risks(analysis),
        _line_table(analysis),
        _alternates(analysis),
        _compliance(analysis),
        _issues(analysis),
        _providers(analysis),
        f'<footer>Generated by {APP_TITLE} v{_e(__version__)} on '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}. All analysis ran '
        f'locally; no BOM data left this machine except the part-number '
        f'lookups sent to the providers listed above.</footer>',
    ])
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_e(analysis.bom.name or "BOM analysis")} — '
        f'{APP_TITLE}</title><style>{_CSS}</style></head><body>'
        f'<div class="wrap">{body}</div><script>{_JS}</script>'
        f'</body></html>'
    )


def write_html(analysis: BomAnalysis, path: str | Path) -> Path:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(analysis), encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"Could not write {path}: {exc}") from exc
    LOG.info("Wrote HTML report to %s", path)
    return path
