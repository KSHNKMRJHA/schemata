"""
The Excel deliverable.

Nine sheets, each answering a question someone actually asks:

``Summary``       what is the state of this BOM, in one page
``Enriched BOM``  every original column, plus everything we learned
``Risk``          the lines to look at first, ranked
``Sourcing``      where to buy each line, and the cheapest basket
``Alternates``    substitutes with scores, concerns and price deltas
``Compliance``    RoHS / REACH / origin / HTS / export control per line
``Issues``        every validation finding, filterable by severity
``Price curve``   unit cost against build quantity
``Providers``     which sources answered, how fast, and what failed

Colour is used sparingly and always with a text label as well, so the sheet
still reads correctly in monochrome print or for a colour-blind reader.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from ..core.errors import ExportError
from ..core.models import (
    Alternate, BomAnalysis, ComplianceState, Lifecycle, LineResult, RiskLevel,
    Severity,
)
from ..util import log
from ..util.text import clean, truncate
from ..util.units import format_lead_time
from ..version import APP_TITLE, __version__
from .xlsx_writer import Workbook

LOG = log.get("export.xlsx")

# Palette: dark slate headers, muted status fills that print legibly.
INK = "111827"
HEADER_BG = "1F2937"
BAND_BG = "F9FAFB"
GOOD_BG, GOOD_FG = "DCFCE7", "14532D"
WARN_BG, WARN_FG = "FEF3C7", "78350F"
BAD_BG, BAD_FG = "FEE2E2", "7F1D1D"
INFO_BG, INFO_FG = "DBEAFE", "1E3A8A"
MUTED_FG = "6B7280"

RISK_FILL = {
    RiskLevel.LOW: (GOOD_BG, GOOD_FG),
    RiskLevel.MEDIUM: (WARN_BG, WARN_FG),
    RiskLevel.HIGH: ("FFE4D5", "7C2D12"),
    RiskLevel.CRITICAL: (BAD_BG, BAD_FG),
}

LIFECYCLE_FILL = {
    Lifecycle.ACTIVE: (GOOD_BG, GOOD_FG),
    Lifecycle.NEW: (INFO_BG, INFO_FG),
    Lifecycle.UNKNOWN: ("F3F4F6", MUTED_FG),
    Lifecycle.NRND: (WARN_BG, WARN_FG),
    Lifecycle.EOL: ("FFE4D5", "7C2D12"),
    Lifecycle.OBSOLETE: (BAD_BG, BAD_FG),
}

SEVERITY_FILL = {
    Severity.ERROR: (BAD_BG, BAD_FG),
    Severity.WARNING: (WARN_BG, WARN_FG),
    Severity.INFO: ("F3F4F6", MUTED_FG),
}

COMPLIANCE_FILL = {
    ComplianceState.COMPLIANT: (GOOD_BG, GOOD_FG),
    ComplianceState.EXEMPT: (WARN_BG, WARN_FG),
    ComplianceState.NON_COMPLIANT: (BAD_BG, BAD_FG),
    ComplianceState.UNKNOWN: ("F3F4F6", MUTED_FG),
}


class ReportBuilder:
    """Builds the workbook. One instance per export."""

    def __init__(self, analysis: BomAnalysis) -> None:
        self.analysis = analysis
        self.wb = Workbook()
        self.currency = analysis.summary.currency or "USD"
        self._make_styles()

    def _make_styles(self) -> None:
        wb = self.wb
        self.s_title = wb.style(bold=True, size=16, color=INK)
        self.s_subtitle = wb.style(size=10, color=MUTED_FG)
        self.s_section = wb.style(bold=True, size=12, color=INK,
                                  border="bottom", border_color="9CA3AF")
        self.s_header = wb.style(bold=True, bg=HEADER_BG, color="FFFFFF",
                                 align="center", valign="center", wrap=True,
                                 border="thin", border_color="374151")
        self.s_label = wb.style(bold=True, color=INK)
        self.s_text = wb.style()
        self.s_muted = wb.style(color=MUTED_FG)
        self.s_wrap = wb.style(wrap=True, valign="top")
        self.s_int = wb.style(number_format="#,##0", align="right")
        self.s_money = wb.style(number_format='#,##0.00', align="right")
        self.s_unit = wb.style(number_format='#,##0.000000', align="right")
        self.s_pct = wb.style(number_format='0.0"%"', align="right")
        self.s_score = wb.style(number_format='0.0', align="right", bold=True)
        self.s_big = wb.style(bold=True, size=28, color=INK, align="center")
        self.s_grade = wb.style(bold=True, size=20, align="center")
        self.s_center = wb.style(align="center")
        self.s_link = wb.style(color="1D4ED8")
        self._fills: dict[tuple[str, str], Any] = {}

    def fill(self, bg: str, fg: str, bold: bool = False, align: str = ""
             ) -> Any:
        key = (bg, fg, bold, align)
        if key not in self._fills:
            self._fills[key] = self.wb.style(bg=bg, color=fg, bold=bold,
                                             align=align)
        return self._fills[key]

    # -- entry point ------------------------------------------------------ #

    def build(self) -> Workbook:
        self._summary_sheet()
        self._bom_sheet()
        self._risk_sheet()
        self._sourcing_sheet()
        self._alternates_sheet()
        self._compliance_sheet()
        self._issues_sheet()
        self._price_curve_sheet()
        self._providers_sheet()
        return self.wb

    def save(self, path: str | Path) -> Path:
        try:
            self.build()
            return self.wb.save(path)
        except OSError as exc:
            raise ExportError(
                f"Could not write the report to {path}: {exc}") from exc

    # -- sheets ----------------------------------------------------------- #

    def _summary_sheet(self) -> None:
        analysis = self.analysis
        summary = analysis.summary
        health = analysis.health
        sheet = self.wb.sheet("Summary", widths=[34, 22, 22, 22, 26, 26],
                              tab_color="2563EB")

        sheet.row([f"{APP_TITLE}"], style=self.s_title)
        sheet.row([f"BOM: {analysis.bom.name or 'unnamed'}"
                   f"    ·    source: {Path(analysis.bom.source_file).name}"
                   f"    ·    generated "
                   f"{_fmt_time(analysis.finished_at)}"
                   f"    ·    engine v{__version__}"],
                  style=self.s_subtitle)
        if analysis.offline:
            sheet.row(["OFFLINE MODE — figures below come from the "
                       "built-in synthetic catalogue, not live distributor "
                       "data."],
                      style=self.fill(WARN_BG, WARN_FG, bold=True))
        sheet.blank()

        # Health block
        sheet.row(["BOM health"], style=self.s_section)
        row = sheet.row([health.score, health.grade, health.level.value,
                         f"{summary.build_quantity:,} unit build"],
                        styles=[self.s_big,
                                self.fill(*RISK_FILL[health.level], bold=True,
                                          align="center"),
                                self.fill(*RISK_FILL[health.level],
                                          align="center"),
                                self.s_center],
                        height=38)
        sheet.row(["Score / 100", "Grade", "Risk level", "Analysed at"],
                  style=self.s_muted)
        sheet.row([health.headline], style=self.s_label)
        if health.drivers:
            sheet.row([f"Main drivers: {'; '.join(health.drivers)}"],
                      style=self.s_wrap)
        sheet.blank()

        sheet.row(["Health components (100 = healthy)"], style=self.s_section)
        sheet.row(["Component", "Score", "Weight"], style=self.s_header)
        from ..analysis.health import COMPONENT_LABELS

        for key, value in sorted(health.components.items(),
                                 key=lambda kv: kv[1]):
            level = RiskLevel.from_score(100.0 - value)
            sheet.row([COMPONENT_LABELS.get(key, key), value,
                       health.weights.get(key, 1.0)],
                      styles=[self.s_text,
                              self.fill(*RISK_FILL[level], align="right"),
                              self.s_center])
        sheet.blank()

        # Key figures
        sheet.row(["Key figures"], style=self.s_section)
        sheet.row(["Metric", "Value", "Notes"], style=self.s_header)
        figures: list[tuple[str, Any, str, Any]] = [
            ("BOM lines", summary.total_lines, "", self.s_int),
            ("Unique part numbers", summary.unique_parts, "", self.s_int),
            ("Placements per assembly", summary.placements, "", self.s_int),
            ("Do-not-populate lines", summary.dnp_lines,
             "excluded from cost and stock checks", self.s_int),
            ("Lines matched to catalogue data", summary.matched_lines,
             f"{_pct(summary.matched_lines, summary.total_lines)} of lines",
             self.s_int),
            ("Lines with no catalogue data", summary.unmatched_lines,
             "not costed or risk-scored", self.s_int),
            ("Matches needing review", summary.review_lines,
             "below your confidence threshold", self.s_int),
            ("Build quantity", summary.build_quantity, "assemblies",
             self.s_int),
            (f"Total material cost ({self.currency})",
             _num(summary.total_cost),
             f"{summary.cost_coverage_pct:.0f}% of lines costed", self.s_money),
            (f"Cost per assembly ({self.currency})",
             _num(summary.cost_per_unit), "", self.s_money),
            (f"Cheapest-source saving ({self.currency})",
             _num(summary.potential_savings),
             "vs buying every line from its dearest source, compared per "
             "piece", self.s_money),
            ("Single-source lines", summary.single_source_lines,
             "only one distributor has stock", self.s_int),
            ("Out-of-stock lines", summary.out_of_stock_lines,
             "no stock at any provider queried", self.s_int),
            ("Long-lead lines", summary.long_lead_lines,
             "at or beyond your lead-time warning threshold", self.s_int),
            ("Worst lead time",
             format_lead_time(summary.max_lead_time_days) or "—", "",
             self.s_center),
            ("Analysis duration", f"{analysis.duration_ms / 1000:.1f} s",
             f"providers: {', '.join(analysis.providers_used)}",
             self.s_center),
        ]
        for label, value, note, style in figures:
            sheet.row([label, value, note],
                      styles=[self.s_text, style, self.s_muted])
        sheet.blank()

        # Distributions
        sheet.row(["Lifecycle distribution"], style=self.s_section)
        sheet.row(["Status", "Lines", "Share"], style=self.s_header)
        for status, count in sorted(summary.lifecycle_counts.items(),
                                    key=lambda kv: -kv[1]):
            try:
                fill = self.fill(*LIFECYCLE_FILL[Lifecycle(status)])
            except ValueError:
                fill = self.s_text
            sheet.row([status, count, _pct(count, summary.total_lines)],
                      styles=[fill, self.s_int, self.s_center])
        sheet.blank()

        sheet.row(["Risk distribution"], style=self.s_section)
        sheet.row(["Level", "Lines", "Share"], style=self.s_header)
        for level in (RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MEDIUM,
                      RiskLevel.LOW):
            count = summary.risk_counts.get(level.value, 0)
            sheet.row([level.value, count, _pct(count, summary.total_lines)],
                      styles=[self.fill(*RISK_FILL[level]), self.s_int,
                              self.s_center])
        sheet.blank()

        if analysis.warnings:
            sheet.row(["Warnings"], style=self.s_section)
            for warning in analysis.warnings:
                sheet.row([warning], style=self.s_wrap)
            sheet.blank()

        bom_issues = [i for i in analysis.issues
                      if i.severity is not Severity.INFO]
        if bom_issues:
            sheet.row(["BOM-level findings"], style=self.s_section)
            sheet.row(["Severity", "Finding", "Suggested action"],
                      style=self.s_header)
            for issue in sorted(bom_issues, key=lambda i: -i.severity.rank):
                sheet.row([issue.severity.value.upper(), issue.message,
                           issue.suggestion or ""],
                          styles=[self.fill(*SEVERITY_FILL[issue.severity],
                                            align="center"),
                                  self.s_wrap, self.s_wrap])

    def _bom_sheet(self) -> None:
        analysis = self.analysis
        extra_columns = sorted({
            key for result in analysis.results for key in result.line.raw
            if key not in set(analysis.bom.column_map.values())
        })
        headers = [
            "#", "Source row", "MPN", "Manufacturer", "Description",
            "Qty/assy", f"Qty for {analysis.summary.build_quantity:,}",
            "Reference designators", "Internal PN", "Value", "Package",
            "DNP", "Match", "Confidence", "Matched MPN", "Matched mfr",
            "Lifecycle", "Lifecycle note", "Total stock", "Stocking dists",
            "Best lead time", f"Unit price ({self.currency})",
            f"Extended ({self.currency})", "Best distributor", "Order qty",
            "Risk", "Risk level", "Flags", "Datasheet", "Issues",
        ] + [f"[src] {name}" for name in extra_columns]

        widths = [6, 10, 26, 22, 44, 10, 14, 26, 18, 14, 14, 7, 12, 11, 26,
                  22, 12, 30, 12, 12, 12, 14, 14, 20, 10, 8, 11, 28, 34, 46]
        widths += [20] * len(extra_columns)
        sheet = self.wb.sheet("Enriched BOM", freeze="C2", widths=widths,
                              tab_color="0EA5E9")
        sheet.row(headers, style=self.s_header, height=32)

        for result in analysis.results:
            line = result.line
            part = result.part
            cost = result.cost
            best = cost.best
            lifecycle = part.lifecycle if part else Lifecycle.UNKNOWN
            stock = part.total_stock if part else None
            stocking = len({o.distributor for o in part.in_stock_offers}) \
                if part else 0
            lead = min((o.lead_time_days for o in part.offers
                        if o.lead_time_days is not None), default=None) \
                if part else None
            issues = "; ".join(
                f"[{i.severity.value[0].upper()}] {i.message}"
                for i in result.all_issues[:6])

            values: list[Any] = [
                line.line_no, line.source_row, line.mpn, line.manufacturer,
                line.description, line.quantity, cost.required_qty,
                line.ref_text, line.internal_pn, line.value, line.package,
                "DNP" if line.dnp else "",
                result.match.kind.value, result.match.confidence,
                result.match.matched_mpn, result.match.matched_manufacturer,
                lifecycle.value if part else "No data",
                part.lifecycle_note if part else "",
                stock, stocking or "",
                format_lead_time(lead) if lead is not None else "",
                _num(cost.unit_price), _num(cost.extended),
                best.distributor if best else "",
                best.order_qty if best else "",
                result.risk.score, result.risk.level.value,
                ", ".join(result.risk.flags),
                part.datasheet_url if part else "",
                issues,
            ]
            values += [line.raw.get(name, "") for name in extra_columns]

            confidence_fill = self.s_center
            if result.match.confidence >= 90:
                confidence_fill = self.fill(GOOD_BG, GOOD_FG, align="center")
            elif result.match.confidence >= 60:
                confidence_fill = self.fill(WARN_BG, WARN_FG, align="center")
            elif result.match.confidence > 0:
                confidence_fill = self.fill(BAD_BG, BAD_FG, align="center")

            styles: list[Any] = [
                self.s_center, self.s_center, self.s_label, self.s_text,
                self.s_text, self.s_int, self.s_int, self.s_text, self.s_text,
                self.s_text, self.s_text,
                self.fill(WARN_BG, WARN_FG, align="center") if line.dnp
                else self.s_center,
                self.s_center, confidence_fill, self.s_text, self.s_text,
                self.fill(*LIFECYCLE_FILL.get(
                    lifecycle, ("F3F4F6", MUTED_FG)), align="center")
                if part else self.fill("F3F4F6", MUTED_FG, align="center"),
                self.s_muted, self.s_int, self.s_center, self.s_center,
                self.s_unit, self.s_money, self.s_text, self.s_int,
                self.fill(*RISK_FILL[result.risk.level], align="right",
                          bold=True),
                self.fill(*RISK_FILL[result.risk.level], align="center"),
                self.s_wrap, self.s_link, self.s_wrap,
            ]
            styles += [self.s_text] * len(extra_columns)

            links: list[str | None] = [None] * len(values)
            if part and part.datasheet_url.startswith("http"):
                links[28] = part.datasheet_url
            sheet.row(values, styles=styles, hyperlinks=links)

        sheet.set_autofilter(1, 0, max(1, sheet.row_count), len(headers) - 1)

    def _risk_sheet(self) -> None:
        from ..analysis.health import top_risks

        sheet = self.wb.sheet("Risk", freeze="A2",
                              widths=[6, 26, 22, 10, 22, 9, 12, 20, 52, 30,
                                      26],
                              tab_color="DC2626")
        sheet.row(["Line", "MPN", "Manufacturer", "Qty", "References",
                   "Score", "Level", "Main driver", "Why", "Flags",
                   "Best alternate"],
                  style=self.s_header, height=30)

        ranked = sorted((r for r in self.analysis.results if not r.line.dnp),
                        key=lambda r: (-r.risk.score, r.line.line_no))
        for result in ranked:
            worst = max(result.risk.factors, key=lambda f: f.score,
                        default=None)
            sheet.row([
                result.line.line_no, result.line.mpn, result.line.manufacturer,
                result.line.quantity, truncate(result.line.ref_text, 40),
                result.risk.score, result.risk.level.value,
                worst.label if worst else "", worst.detail if worst else "",
                ", ".join(result.risk.flags),
                result.alternates[0].mpn if result.alternates else "",
            ], styles=[
                self.s_center, self.s_label, self.s_text, self.s_int,
                self.s_text,
                self.fill(*RISK_FILL[result.risk.level], align="right",
                          bold=True),
                self.fill(*RISK_FILL[result.risk.level], align="center"),
                self.s_text, self.s_wrap, self.s_wrap, self.s_text,
            ])
        sheet.set_autofilter(1, 0, max(1, sheet.row_count), 10)

        sheet.blank(2)
        sheet.row(["Risk factor detail for the ten highest-risk lines"],
                  style=self.s_section)
        sheet.row(["Line", "MPN", "Factor", "Score", "Weight", "Detail"],
                  style=self.s_header)
        for entry in top_risks(self.analysis.results, limit=10):
            result = self.analysis.result_for_line(int(entry["line_no"]))
            if result is None:
                continue
            for factor in sorted(result.risk.factors,
                                 key=lambda f: -f.score):
                level = RiskLevel.from_score(factor.score)
                sheet.row([result.line.line_no, result.line.mpn, factor.label,
                           round(factor.score, 1), factor.weight,
                           factor.detail],
                          styles=[self.s_center, self.s_text, self.s_text,
                                  self.fill(*RISK_FILL[level], align="right"),
                                  self.s_center, self.s_wrap])

    def _sourcing_sheet(self) -> None:
        sheet = self.wb.sheet(
            "Sourcing", freeze="A2",
            widths=[6, 26, 22, 12, 22, 22, 12, 12, 10, 10, 14, 14, 14, 12,
                    44, 40],
            tab_color="059669")
        sheet.row(["Line", "MPN", "Manufacturer", "Need qty", "Distributor",
                   "Distributor SKU", "Stock", "Lead time", "MOQ", "Pack",
                   "Order qty", f"Unit ({self.currency})",
                   f"Extended ({self.currency})", "Break qty", "Notes",
                   "Buy link"],
                  style=self.s_header, height=30)

        for result in self.analysis.results:
            if result.line.dnp or not result.cost.options:
                continue
            for index, option in enumerate(result.cost.options):
                is_best = index == 0
                row_style = self.fill(GOOD_BG, GOOD_FG) if is_best \
                    else self.s_text
                sheet.row([
                    result.line.line_no if is_best else "",
                    result.line.mpn if is_best else "",
                    result.line.manufacturer if is_best else "",
                    result.cost.required_qty if is_best else "",
                    option.distributor, option.sku,
                    option.stock, format_lead_time(option.lead_time_days),
                    option.moq or "", option.spq or "", option.order_qty,
                    _num(option.unit_price), _num(option.extended),
                    option.break_qty or "",
                    "; ".join(option.notes),
                    option.url,
                ], styles=[
                    self.s_center, self.s_label, self.s_text, self.s_int,
                    row_style, self.s_muted, self.s_int, self.s_center,
                    self.s_center, self.s_center, self.s_int, self.s_unit,
                    self.s_money, self.s_center, self.s_wrap, self.s_link,
                ], hyperlinks=[None] * 15 + [
                    option.url if option.url.startswith("http") else None])
        sheet.set_autofilter(1, 0, max(1, sheet.row_count), 15)

        uncosted = [r for r in self.analysis.results
                    if not r.line.dnp and r.cost.extended is None]
        if uncosted:
            sheet.blank(2)
            sheet.row([f"{len(uncosted)} line(s) could not be costed"],
                      style=self.s_section)
            sheet.row(["Line", "MPN", "Reason"], style=self.s_header)
            for result in uncosted:
                reason = "; ".join(result.cost.notes) or \
                    "No offer or price was available."
                sheet.row([result.line.line_no, result.line.mpn, reason],
                          styles=[self.s_center, self.s_label, self.s_wrap])

    def _alternates_sheet(self) -> None:
        sheet = self.wb.sheet(
            "Alternates", freeze="A2",
            widths=[6, 26, 14, 26, 22, 44, 8, 12, 12, 12, 14, 12, 44, 44],
            tab_color="7C3AED")
        sheet.row(["Line", "Original MPN", "Orig. status", "Alternate MPN",
                   "Manufacturer", "Description", "Score", "Drop-in",
                   "Lifecycle", "Stock", f"Unit ({self.currency})",
                   "Price Δ", "Why it fits", "Concerns"],
                  style=self.s_header, height=30)

        any_rows = False
        for result in self.analysis.results:
            if not result.alternates:
                continue
            any_rows = True
            original_status = result.part.lifecycle.value if result.part \
                else "No data"
            for alternate in result.alternates:
                sheet.row([
                    result.line.line_no, result.line.mpn, original_status,
                    alternate.mpn, alternate.manufacturer,
                    truncate(alternate.description, 90), alternate.score,
                    _dropin_label(alternate), alternate.lifecycle.value,
                    alternate.stock, _num(alternate.unit_price),
                    f"{alternate.price_delta_pct:+.0f}%"
                    if alternate.price_delta_pct is not None else "",
                    "; ".join(alternate.reasons),
                    "; ".join(alternate.concerns),
                ], styles=[
                    self.s_center, self.s_label, self.s_muted, self.s_label,
                    self.s_text, self.s_text,
                    self.fill(*_score_fill(alternate.score), align="right",
                              bold=True),
                    self.s_center,
                    self.fill(*LIFECYCLE_FILL.get(
                        alternate.lifecycle, ("F3F4F6", MUTED_FG)),
                        align="center"),
                    self.s_int, self.s_unit, self.s_center, self.s_wrap,
                    self.s_wrap,
                ])
        if not any_rows:
            sheet.row(["No alternates were needed: every populated line has "
                       "an active lifecycle status, enough stock and multiple "
                       "sources."], style=self.s_muted)
        else:
            sheet.set_autofilter(1, 0, max(1, sheet.row_count), 13)

    def _compliance_sheet(self) -> None:
        sheet = self.wb.sheet(
            "Compliance", freeze="A2",
            widths=[6, 26, 22, 7, 14, 30, 14, 34, 12, 8, 16, 12, 12, 12, 30],
            tab_color="D97706")
        sheet.row(["Line", "MPN", "Manufacturer", "Qty", "RoHS", "RoHS note",
                   "REACH", "SVHC substances", "Halogen free", "MSL",
                   "Country of origin", "HTS code", "ECCN",
                   "Export controlled", "AEC-Q / notes"],
                  style=self.s_header, height=30)

        for result in self.analysis.results:
            compliance = result.compliance
            export = "ITAR" if compliance.itar else (
                "Yes" if compliance.export_controlled else "No")
            export_style = self.fill(BAD_BG, BAD_FG, align="center") \
                if compliance.itar else (
                self.fill(WARN_BG, WARN_FG, align="center")
                if compliance.export_controlled else self.s_center)
            sheet.row([
                result.line.line_no, result.line.mpn,
                result.line.manufacturer, result.line.quantity,
                compliance.rohs.value, compliance.rohs_note,
                compliance.reach.value, "; ".join(compliance.svhc),
                compliance.halogen_free.value, compliance.msl,
                compliance.country_of_origin, compliance.hts_code,
                compliance.eccn, export,
                compliance.aec_q or compliance.lead_free_process,
            ], styles=[
                self.s_center, self.s_label, self.s_text, self.s_int,
                self.fill(*COMPLIANCE_FILL[compliance.rohs], align="center"),
                self.s_muted,
                self.fill(*COMPLIANCE_FILL[compliance.reach], align="center"),
                self.s_wrap, self.s_center, self.s_center, self.s_center,
                self.s_center, self.s_center, export_style, self.s_text,
            ])
        sheet.set_autofilter(1, 0, max(1, sheet.row_count), 14)

        sheet.blank(2)
        sheet.row(["Rules applied"], style=self.s_section)
        settings_rows = [
            ("RoHS required", "Yes" if _rule(self.analysis, "require_rohs")
             else "No"),
            ("REACH required", "Yes" if _rule(self.analysis, "require_reach")
             else "No"),
            ("Export control flagged",
             "Yes" if _rule(self.analysis, "flag_export_controlled") else "No"),
        ]
        for label, value in settings_rows:
            sheet.row([label, value], styles=[self.s_label, self.s_center])
        sheet.blank()
        sheet.row(["Compliance data is reported as supplied by the "
                   "distributors queried. Unknown means no declaration was "
                   "found, not that the part is compliant. Confirm against "
                   "the manufacturer's declaration before shipping."],
                  style=self.s_wrap)

    def _issues_sheet(self) -> None:
        sheet = self.wb.sheet("Issues", freeze="A2",
                              widths=[10, 6, 26, 22, 70, 60, 14],
                              tab_color="B45309")
        sheet.row(["Severity", "Line", "MPN", "Field", "Finding",
                   "Suggested action", "Code"],
                  style=self.s_header, height=28)

        rows: list[tuple[Any, ...]] = []
        for issue in self.analysis.issues:
            rows.append((issue.severity, "", "", issue.field or "",
                         issue.message, issue.suggestion or "", issue.code))
        for result in self.analysis.results:
            for issue in result.all_issues:
                rows.append((issue.severity, result.line.line_no,
                             result.line.mpn, issue.field or "", issue.message,
                             issue.suggestion or "", issue.code))
        rows.sort(key=lambda row: (-row[0].rank, row[1] or 0))

        for severity, line_no, mpn, field, message, suggestion, code in rows:
            sheet.row([severity.value.upper(), line_no, mpn, field, message,
                       suggestion, code],
                      styles=[self.fill(*SEVERITY_FILL[severity],
                                        align="center", bold=True),
                              self.s_center, self.s_label, self.s_muted,
                              self.s_wrap, self.s_wrap, self.s_muted])
        if not rows:
            sheet.row(["No issues were found."],
                      style=self.fill(GOOD_BG, GOOD_FG))
        else:
            sheet.set_autofilter(1, 0, max(1, sheet.row_count), 6)

    def _price_curve_sheet(self) -> None:
        sheet = self.wb.sheet("Price curve", freeze="A2",
                              widths=[6, 26, 14, 14, 16, 16, 22],
                              tab_color="0891B2")
        sheet.row(["Line", "MPN", "Build qty", "Line qty",
                   f"Unit ({self.currency})", f"Extended ({self.currency})",
                   "Cheapest distributor"],
                  style=self.s_header, height=28)
        wrote = False
        for result in self.analysis.results:
            for point in result.cost.price_curve:
                wrote = True
                sheet.row([
                    result.line.line_no, result.line.mpn, point["build_qty"],
                    point["line_qty"], Decimal(str(point["unit_price"])),
                    Decimal(str(point["extended"])), point["distributor"],
                ], styles=[self.s_center, self.s_label, self.s_int,
                           self.s_int, self.s_unit, self.s_money, self.s_text])
        if not wrote:
            sheet.row(["No price ladders were available to build a curve."],
                      style=self.s_muted)
        else:
            sheet.set_autofilter(1, 0, max(1, sheet.row_count), 6)
            sheet.blank(2)
            sheet.row(["Unit price is the cheapest offer at that build "
                       "quantity, with any minimum-order overbuy spread across "
                       "the pieces actually needed."], style=self.s_wrap)

    def _providers_sheet(self) -> None:
        sheet = self.wb.sheet("Providers", widths=[20, 26, 10, 10, 10, 12, 10,
                                                   14, 14, 60],
                              tab_color="4B5563")
        sheet.row(["Provider", "Name", "Calls", "Hits", "Misses", "Cache hits",
                   "Errors", "Avg latency", "Hit rate", "Notes"],
                  style=self.s_header, height=28)
        stats = self.analysis.summary.provider_stats or {}
        for provider_id, data in sorted(stats.items()):
            if provider_id.startswith("_"):
                continue
            notes = data.get("skipped") or data.get("disabled_reason") or \
                data.get("last_error") or ""
            sheet.row([
                provider_id, data.get("name", ""), data.get("calls", 0),
                data.get("hits", 0), data.get("misses", 0),
                data.get("cache_hits", 0), data.get("errors", 0),
                f"{data.get('avg_latency_ms', 0)} ms",
                f"{data.get('hit_rate_pct', 0)}%", truncate(str(notes), 220),
            ], styles=[self.s_label, self.s_text, self.s_int, self.s_int,
                       self.s_int, self.s_int,
                       self.fill(BAD_BG, BAD_FG, align="right")
                       if data.get("errors") else self.s_int,
                       self.s_center, self.s_center, self.s_wrap])
        http = stats.get("_http") or {}
        if http:
            sheet.blank()
            sheet.row(["HTTP totals"], style=self.s_section)
            for key, value in http.items():
                sheet.row([key.replace("_", " ").capitalize(), value],
                          styles=[self.s_text, self.s_int])
        sheet.blank()
        sheet.row([f"Report generated by {APP_TITLE} v{__version__} on "
                   f"{datetime.now().strftime('%Y-%m-%d %H:%M')}. "
                   f"All analysis ran locally on this machine."],
                  style=self.s_muted)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _num(value: Any) -> Any:
    if value is None or value == "":
        return ""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover
        return value


def _pct(part: int, whole: int) -> str:
    if not whole:
        return ""
    return f"{100.0 * part / whole:.0f}%"


def _fmt_time(value: str) -> str:
    text = clean(value)
    if not text:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    return text.replace("T", " ").replace("Z", " UTC")


def _score_fill(score: int) -> tuple[str, str]:
    if score >= 75:
        return GOOD_BG, GOOD_FG
    if score >= 55:
        return WARN_BG, WARN_FG
    return BAD_BG, BAD_FG


def _dropin_label(alternate: Alternate) -> str:
    if alternate.pin_compatible is True:
        return "Yes"
    if alternate.pin_compatible is False:
        return "No"
    return "Check"


_RULE_DEFAULTS = {"require_rohs": True, "require_reach": False,
                  "flag_export_controlled": True}


def _rule(analysis: BomAnalysis, key: str) -> bool:
    """Read a rule the analysis recorded, falling back to the default."""
    rules = analysis.summary.rules or {}
    return bool(rules.get(key, _RULE_DEFAULTS.get(key, False)))


def write_report(analysis: BomAnalysis, path: str | Path) -> Path:
    """Build and save the Excel report."""
    builder = ReportBuilder(analysis)
    written = builder.save(path)
    LOG.info("Wrote Excel report to %s (%d bytes)", written,
             written.stat().st_size)
    return written
