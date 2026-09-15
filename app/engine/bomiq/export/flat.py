"""
Flat exporters: CSV, JSON and a purchase-order style quote request.

These are the formats other systems consume -- ERP imports, distributor BOM
upload tools, scripts. Each one is deliberately boring and stable: one header
row, no merged cells, no colour, UTF-8 with a BOM so Excel opens it correctly
on Windows without a wizard.
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.errors import ExportError
from ..core.models import BomAnalysis, Lifecycle, LineResult
from ..util import log
from ..util.text import clean
from ..util.units import format_lead_time
from ..version import __version__

LOG = log.get("export.flat")

ENRICHED_COLUMNS = [
    ("line_no", "Line"),
    ("source_row", "Source row"),
    ("mpn", "MPN"),
    ("manufacturer", "Manufacturer"),
    ("description", "Description"),
    ("quantity", "Qty per assembly"),
    ("required_qty", "Qty for build"),
    ("ref_designators", "Reference designators"),
    ("internal_pn", "Internal PN"),
    ("value", "Value"),
    ("package", "Package"),
    ("dnp", "DNP"),
    ("match_kind", "Match type"),
    ("match_confidence", "Match confidence"),
    ("matched_mpn", "Matched MPN"),
    ("lifecycle", "Lifecycle"),
    ("lifecycle_note", "Lifecycle note"),
    ("total_stock", "Total stock"),
    ("stocking_distributors", "Stocking distributors"),
    ("best_lead_time_days", "Best lead time (days)"),
    ("unit_price", "Unit price"),
    ("extended_price", "Extended price"),
    ("currency", "Currency"),
    ("best_distributor", "Best distributor"),
    ("best_sku", "Best distributor SKU"),
    ("order_qty", "Order qty"),
    ("moq", "MOQ"),
    ("spq", "Pack size"),
    ("risk_score", "Risk score"),
    ("risk_level", "Risk level"),
    ("flags", "Flags"),
    ("rohs", "RoHS"),
    ("reach", "REACH"),
    ("country_of_origin", "Country of origin"),
    ("hts_code", "HTS code"),
    ("eccn", "ECCN"),
    ("best_alternate", "Best alternate"),
    ("best_alternate_score", "Alternate score"),
    ("datasheet_url", "Datasheet"),
    ("buy_url", "Buy link"),
    ("issues", "Issues"),
]


def line_record(result: LineResult, currency: str) -> dict[str, Any]:
    """Flatten one analysed line into a single, exportable record."""
    line = result.line
    part = result.part
    cost = result.cost
    best = cost.best
    lead_times = [o.lead_time_days for o in (part.offers if part else [])
                  if o.lead_time_days is not None]
    alternate = result.alternates[0] if result.alternates else None

    return {
        "line_no": line.line_no,
        "source_row": line.source_row or "",
        "mpn": line.mpn,
        "manufacturer": line.manufacturer,
        "description": line.description,
        "quantity": _number(line.quantity),
        "required_qty": cost.required_qty,
        "ref_designators": line.ref_text,
        "internal_pn": line.internal_pn,
        "value": line.value,
        "package": line.package,
        "dnp": "Y" if line.dnp else "",
        "match_kind": result.match.kind.value,
        "match_confidence": result.match.confidence,
        "matched_mpn": result.match.matched_mpn,
        "lifecycle": part.lifecycle.value if part else "No data",
        "lifecycle_note": part.lifecycle_note if part else "",
        "total_stock": part.total_stock if part else "",
        "stocking_distributors": len({o.distributor
                                      for o in part.in_stock_offers})
        if part else 0,
        "best_lead_time_days": min(lead_times) if lead_times else "",
        "unit_price": _number(cost.unit_price),
        "extended_price": _number(cost.extended),
        "currency": currency,
        "best_distributor": best.distributor if best else "",
        "best_sku": best.sku if best else "",
        "order_qty": best.order_qty if best else "",
        "moq": best.moq if best and best.moq else "",
        "spq": best.spq if best and best.spq else "",
        "risk_score": result.risk.score,
        "risk_level": result.risk.level.value,
        "flags": ", ".join(result.risk.flags),
        "rohs": result.compliance.rohs.value,
        "reach": result.compliance.reach.value,
        "country_of_origin": result.compliance.country_of_origin,
        "hts_code": result.compliance.hts_code,
        "eccn": result.compliance.eccn,
        "best_alternate": alternate.mpn if alternate else "",
        "best_alternate_score": alternate.score if alternate else "",
        "datasheet_url": part.datasheet_url if part else "",
        "buy_url": best.url if best else "",
        "issues": " | ".join(
            f"[{i.severity.value[0].upper()}] {i.message}"
            for i in result.all_issues[:8]),
    }


# Leading characters that make a spreadsheet treat a cell as a formula. A BOM
# is untrusted input, so a cell such as ``=cmd|'/c calc'!A1`` must not be
# re-exported in a form Excel will execute when the buyer opens the report.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in an exported cell."""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _FORMULA_PREFIXES:
        # A leading apostrophe is the standard, lossless way to force text.
        return "'" + value
    return value


def _number(value: Any) -> Any:
    if value is None or value == "":
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

def write_csv(analysis: BomAnalysis, path: str | Path,
              include_source_columns: bool = True,
              delimiter: str = ",") -> Path:
    """Write the enriched BOM as a single CSV."""
    path = Path(path)
    currency = analysis.summary.currency or "USD"
    extra = sorted({
        key for result in analysis.results for key in result.line.raw
    }) if include_source_columns else []

    headers = [label for _, label in ENRICHED_COLUMNS] + \
        [f"[src] {name}" for name in extra]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter,
                                quoting=csv.QUOTE_MINIMAL)
            writer.writerow(headers)
            for result in analysis.results:
                record = line_record(result, currency)
                row = [record.get(key, "") for key, _ in ENRICHED_COLUMNS]
                row += [result.line.raw.get(name, "") for name in extra]
                writer.writerow([csv_safe(cell) for cell in row])
    except OSError as exc:
        raise ExportError(f"Could not write {path}: {exc}") from exc
    LOG.info("Wrote CSV to %s", path)
    return path


def write_issues_csv(analysis: BomAnalysis, path: str | Path) -> Path:
    """Write every finding as its own row -- handy for issue tracking."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Severity", "Scope", "Line", "MPN", "Field",
                             "Code", "Finding", "Suggested action"])
            for issue in analysis.issues:
                writer.writerow([issue.severity.value, "BOM", "", "",
                                 issue.field or "", issue.code, issue.message,
                                 issue.suggestion or ""])
            for result in analysis.results:
                for issue in result.all_issues:
                    writer.writerow([csv_safe(cell) for cell in [
                        issue.severity.value, "Line", result.line.line_no,
                        result.line.mpn, issue.field or "", issue.code,
                        issue.message, issue.suggestion or "",
                    ]])
    except OSError as exc:
        raise ExportError(f"Could not write {path}: {exc}") from exc
    return path


def write_quote_request_csv(analysis: BomAnalysis, path: str | Path) -> Path:
    """A minimal ``MPN, Manufacturer, Quantity`` file for distributor upload.

    This is the format DigiKey, Mouser and Farnell BOM tools accept, so a buyer
    can go straight from the analysis to a live quote.
    """
    path = Path(path)
    build = max(1, analysis.summary.build_quantity)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Manufacturer Part Number", "Manufacturer",
                             "Quantity", "Customer Reference", "Description"])
            for result in analysis.results:
                line = result.line
                if line.dnp or not clean(line.mpn):
                    continue
                quantity = result.cost.required_qty or int(
                    line.effective_quantity * build)
                writer.writerow([csv_safe(cell) for cell in [
                    line.mpn,
                    line.manufacturer or (result.part.manufacturer
                                          if result.part else ""),
                    quantity,
                    line.internal_pn or line.ref_text,
                    line.description,
                ]])
    except OSError as exc:
        raise ExportError(f"Could not write {path}: {exc}") from exc
    return path


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def write_json(analysis: BomAnalysis, path: str | Path,
               full: bool = True, indent: int = 2) -> Path:
    """Write the analysis as JSON.

    ``full=True`` writes everything (round-trips back into the app);
    ``full=False`` writes the flattened per-line records plus the summary,
    which is what most downstream scripts want.
    """
    path = Path(path)
    if full:
        payload: dict[str, Any] = analysis.to_dict()
        payload["format"] = "bomiq.analysis.v1"
        payload["engine_version"] = __version__
    else:
        currency = analysis.summary.currency or "USD"
        payload = {
            "format": "bomiq.flat.v1",
            "engine_version": __version__,
            "bom": {
                "name": analysis.bom.name,
                "source_file": analysis.bom.source_file,
                "lines": analysis.bom.line_count,
                "metadata": analysis.bom.metadata,
            },
            "summary": analysis.summary.to_dict(),
            "health": analysis.health.to_dict(),
            "offline": analysis.offline,
            "warnings": analysis.warnings,
            "issues": [issue.to_dict() for issue in analysis.issues],
            "lines": [line_record(result, currency)
                      for result in analysis.results],
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=indent, default=str),
                        encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"Could not write {path}: {exc}") from exc
    LOG.info("Wrote JSON to %s", path)
    return path


# --------------------------------------------------------------------------- #
# Console table
# --------------------------------------------------------------------------- #

def render_table(rows: Sequence[Sequence[Any]], headers: Sequence[str],
                 max_width: int = 200) -> str:
    """Plain-text table for the CLI. No dependencies, aligns to content."""
    all_rows = [list(map(_cell_text, headers))] + \
        [list(map(_cell_text, row)) for row in rows]
    columns = max((len(row) for row in all_rows), default=0)
    for row in all_rows:
        row.extend([""] * (columns - len(row)))
    widths = [
        min(48, max(len(row[index]) for row in all_rows))
        for index in range(columns)
    ]
    # Shrink the widest columns until the table fits.
    while sum(widths) + 3 * (columns - 1) > max_width and max(widths) > 8:
        widest = widths.index(max(widths))
        widths[widest] -= 1

    def render(row: Sequence[str], pad: str = " ") -> str:
        return "  ".join(
            _fit(value, widths[index]) for index, value in enumerate(row)
        ).rstrip()

    lines = [render(all_rows[0]),
             "  ".join("-" * width for width in widths)]
    for row in all_rows[1:]:
        lines.append(render(row))
    return "\n".join(lines)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def summary_text(analysis: BomAnalysis) -> str:
    """A compact console summary used by the CLI."""
    summary = analysis.summary
    health = analysis.health
    lines = [
        f"BOM            {analysis.bom.name or '(unnamed)'}",
        f"Health         {health.score:.1f}/100  grade {health.grade}  "
        f"({health.level.value})",
        f"               {health.headline}",
        f"Lines          {summary.total_lines} "
        f"({summary.dnp_lines} DNP, {summary.unique_parts} unique parts, "
        f"{summary.placements} placements)",
        f"Matched        {summary.matched_lines}/{summary.total_lines}"
        f"   unmatched {summary.unmatched_lines}"
        f"   need review {summary.review_lines}",
        f"Cost           {summary.total_cost or '-'} {summary.currency} for "
        f"{summary.build_quantity} unit(s)"
        f"   ({summary.cost_per_unit or '-'} per unit, "
        f"{summary.cost_coverage_pct:.0f}% costed)",
        f"Supply         {summary.out_of_stock_lines} out of stock, "
        f"{summary.single_source_lines} single-source, "
        f"{summary.long_lead_lines} long lead",
        f"Risk           " + ", ".join(
            f"{level}: {count}" for level, count
            in sorted(summary.risk_counts.items())) or "n/a",
        f"Providers      {', '.join(analysis.providers_used)}"
        f"{'  [OFFLINE - synthetic data]' if analysis.offline else ''}",
        f"Duration       {analysis.duration_ms / 1000:.1f} s",
    ]
    if health.drivers:
        lines.append("Drivers        " + "; ".join(health.drivers))
    for warning in analysis.warnings:
        lines.append(f"Warning        {warning}")
    return "\n".join(lines)


def issue_lines(analysis: BomAnalysis, min_severity: str = "warning",
                limit: int = 40) -> list[str]:
    """Formatted issue list for the CLI."""
    from ..core.models import Severity

    threshold = Severity(min_severity).rank
    out: list[str] = []
    for issue in analysis.issues:
        if issue.severity.rank >= threshold:
            out.append(f"  [{issue.severity.value.upper():7s}] BOM      "
                       f"{issue.message}")
    for result in analysis.results:
        for issue in result.all_issues:
            if issue.severity.rank >= threshold:
                out.append(
                    f"  [{issue.severity.value.upper():7s}] "
                    f"line {result.line.line_no:<4d} {issue.message}")
    return out[:limit]


def iter_export_formats() -> Iterable[tuple[str, str]]:
    return (
        ("xlsx", "Full Excel workbook (9 sheets)"),
        ("csv", "Enriched BOM as one CSV"),
        ("issues-csv", "Every validation finding as a CSV"),
        ("quote-csv", "MPN/qty file for distributor BOM upload"),
        ("json", "Complete analysis as JSON"),
        ("json-flat", "Flattened per-line JSON for scripts"),
        ("html", "Self-contained HTML report"),
    )
