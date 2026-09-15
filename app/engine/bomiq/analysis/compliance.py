"""
Compliance analysis: RoHS, REACH/SVHC, halogen-free, conflict minerals,
country of origin, HTS classification and export control.

The engine's job here is not to give legal advice -- it is to make the
*evidence* visible: what each source said, where the gaps are, and which lines
would block a shipment under the project's rules. Anything unknown is reported
as unknown rather than assumed compliant, because "no data" is the most common
and most dangerous state in real BOMs.
"""

from __future__ import annotations

from typing import Sequence

from ..config import Settings
from ..core.models import (
    Compliance, ComplianceState, Issue, LineResult, PartData, Severity,
)
from ..util.text import clean

# Countries whose sourcing commonly triggers extra review in the sectors this
# tool is aimed at. Informational only -- no judgement is implied, and the list
# is user-visible so it can be discussed rather than hidden in code.
REVIEW_ORIGINS: dict[str, str] = {
    "RU": "sanctions screening required in most jurisdictions",
    "BY": "sanctions screening required in most jurisdictions",
    "IR": "sanctions screening required in most jurisdictions",
    "KP": "sanctions screening required in most jurisdictions",
    "SY": "sanctions screening required in most jurisdictions",
    "CU": "sanctions screening required in most jurisdictions",
}

ITAR_HINTS = ("itar", "usml", "defense article", "military critical")
EXPORT_HINTS = ("eccn", "export control", "export licen", "dual use",
                "wassenaar")


def resolve(part: PartData | None, settings: Settings) -> Compliance:
    """Return the compliance record for a line, with gaps made explicit."""
    if part is None:
        compliance = Compliance()
        compliance.rohs_note = "No catalogue record"
        return compliance
    compliance = part.compliance

    # Infer export control from free text if a provider did not set the flag.
    if compliance.export_controlled is None:
        haystack = " ".join([
            compliance.eccn, compliance.rohs_note, compliance.reach_note,
            *[f"{k} {v}" for k, v in part.specs.items()
              if "export" in k.lower() or "eccn" in k.lower()],
        ]).lower()
        if any(hint in haystack for hint in ITAR_HINTS):
            compliance.itar = True
            compliance.export_controlled = True
        elif clean(compliance.eccn) and clean(compliance.eccn).upper() != "EAR99":
            compliance.export_controlled = True
        elif any(hint in haystack for hint in EXPORT_HINTS) and \
                "ear99" not in haystack:
            compliance.export_controlled = True
        else:
            compliance.export_controlled = False
    return compliance


def line_issues(result: LineResult, settings: Settings) -> list[Issue]:
    """Compliance findings for one line, phrased as actions."""
    issues: list[Issue] = []
    line = result.line
    if line.dnp:
        return issues
    compliance = result.compliance
    part = result.part

    if part is None:
        return issues

    if compliance.rohs is ComplianceState.NON_COMPLIANT:
        issues.append(Issue(
            code="rohs_non_compliant",
            message=f"{line.mpn} is not RoHS compliant"
                    f"{f' ({compliance.rohs_note})' if compliance.rohs_note else ''}.",
            severity=Severity.ERROR if settings.require_rohs else Severity.WARNING,
            line_no=line.line_no, field="compliance",
            suggestion="Substitute a compliant part, or record an exemption "
                       "for this product.",
        ))
    elif compliance.rohs is ComplianceState.EXEMPT:
        issues.append(Issue(
            code="rohs_exempt",
            message=f"{line.mpn} is RoHS compliant only by exemption"
                    f"{f': {compliance.rohs_note}' if compliance.rohs_note else ''}.",
            severity=Severity.WARNING,
            line_no=line.line_no, field="compliance",
            suggestion="Check the exemption is still valid for your market and "
                       "shipping date.",
        ))
    elif compliance.rohs is ComplianceState.UNKNOWN and settings.require_rohs:
        issues.append(Issue(
            code="rohs_unknown",
            message=f"No RoHS declaration was found for {line.mpn}.",
            severity=Severity.WARNING, line_no=line.line_no,
            field="compliance",
            suggestion="Request the manufacturer's material declaration.",
        ))

    if compliance.reach is ComplianceState.NON_COMPLIANT:
        substances = ", ".join(compliance.svhc[:3]) or "substances not listed"
        issues.append(Issue(
            code="reach_svhc",
            message=f"{line.mpn} declares REACH substances of very high "
                    f"concern ({substances}).",
            severity=Severity.ERROR if settings.require_reach
            else Severity.WARNING,
            line_no=line.line_no, field="compliance",
            suggestion="Above 0.1% w/w this triggers notification duties in "
                       "the EU — confirm the concentration.",
        ))
    elif compliance.reach is ComplianceState.UNKNOWN and settings.require_reach:
        issues.append(Issue(
            code="reach_unknown",
            message=f"No REACH declaration was found for {line.mpn}.",
            severity=Severity.WARNING, line_no=line.line_no,
            field="compliance",
        ))

    if settings.flag_export_controlled:
        if compliance.itar:
            issues.append(Issue(
                code="itar_controlled",
                message=f"{line.mpn} is flagged as ITAR controlled.",
                severity=Severity.ERROR, line_no=line.line_no,
                field="compliance",
                suggestion="Export of this item needs authorisation; involve "
                           "your trade compliance team before shipping.",
            ))
        elif compliance.export_controlled:
            issues.append(Issue(
                code="export_controlled",
                message=f"{line.mpn} carries export control classification "
                        f"{compliance.eccn or '(unspecified)'}.",
                severity=Severity.WARNING, line_no=line.line_no,
                field="compliance",
                suggestion="Check licence requirements for your destination "
                           "countries.",
            ))

    origin = clean(compliance.country_of_origin).upper()[:2]
    if origin in REVIEW_ORIGINS:
        issues.append(Issue(
            code="origin_review",
            message=f"{line.mpn} is sourced from {origin}: "
                    f"{REVIEW_ORIGINS[origin]}.",
            severity=Severity.WARNING, line_no=line.line_no,
            field="compliance",
        ))

    if not clean(compliance.hts_code) and part.offers:
        issues.append(Issue(
            code="hts_missing",
            message=f"No HTS/tariff code was published for {line.mpn}.",
            severity=Severity.INFO, line_no=line.line_no, field="compliance",
            suggestion="Needed for customs paperwork and duty estimation.",
        ))
    if clean(compliance.msl) and clean(compliance.msl) not in ("1", "MSL1",
                                                               "MSL 1"):
        issues.append(Issue(
            code="msl_handling",
            message=f"{line.mpn} is moisture sensitivity level "
                    f"{compliance.msl} and needs dry storage and bake-out "
                    f"handling.",
            severity=Severity.INFO, line_no=line.line_no, field="compliance",
        ))
    return issues


def summarise(results: Sequence[LineResult], settings: Settings
              ) -> dict[str, object]:
    """Whole-BOM compliance picture."""
    counts = {state.value: 0 for state in ComplianceState}
    reach_counts = {state.value: 0 for state in ComplianceState}
    origins: dict[str, int] = {}
    blockers: list[dict[str, object]] = []
    svhc_parts: list[dict[str, object]] = []
    export_parts: list[dict[str, object]] = []
    missing_declaration = 0
    considered = 0

    for result in results:
        if result.line.dnp:
            continue
        considered += 1
        compliance = result.compliance
        counts[compliance.rohs.value] = counts.get(compliance.rohs.value, 0) + 1
        reach_counts[compliance.reach.value] = \
            reach_counts.get(compliance.reach.value, 0) + 1
        origin = clean(compliance.country_of_origin).upper()[:2] or "??"
        origins[origin] = origins.get(origin, 0) + 1

        if compliance.rohs is ComplianceState.UNKNOWN:
            missing_declaration += 1
        if compliance.rohs is ComplianceState.NON_COMPLIANT and \
                settings.require_rohs:
            blockers.append({
                "line_no": result.line.line_no,
                "mpn": result.line.mpn,
                "reason": "Not RoHS compliant",
            })
        if compliance.reach is ComplianceState.NON_COMPLIANT:
            svhc_parts.append({
                "line_no": result.line.line_no,
                "mpn": result.line.mpn,
                "substances": list(compliance.svhc)[:5],
            })
            if settings.require_reach:
                blockers.append({
                    "line_no": result.line.line_no,
                    "mpn": result.line.mpn,
                    "reason": "REACH SVHC declared",
                })
        if compliance.itar or compliance.export_controlled:
            export_parts.append({
                "line_no": result.line.line_no,
                "mpn": result.line.mpn,
                "eccn": compliance.eccn,
                "itar": bool(compliance.itar),
            })

    return {
        "lines_considered": considered,
        "rohs": counts,
        "reach": reach_counts,
        "origins": dict(sorted(origins.items(), key=lambda kv: -kv[1])),
        "blockers": blockers,
        "svhc_parts": svhc_parts,
        "export_controlled": export_parts,
        "missing_declarations": missing_declaration,
        "declaration_coverage_pct": round(
            100.0 * (considered - missing_declaration) / considered, 1)
        if considered else 0.0,
        "rules": {
            "require_rohs": settings.require_rohs,
            "require_reach": settings.require_reach,
            "flag_export_controlled": settings.flag_export_controlled,
        },
    }


def bom_issues(summary: dict[str, object], settings: Settings) -> list[Issue]:
    """Roll compliance findings up to BOM level."""
    issues: list[Issue] = []
    blockers = summary.get("blockers") or []
    if blockers:
        listing = ", ".join(
            f"{item['mpn']} (line {item['line_no']})" for item in blockers[:6])
        issues.append(Issue(
            code="compliance_blockers",
            message=f"{len(blockers)} line(s) would fail your compliance "
                    f"rules: {listing}"
                    f"{' and others' if len(blockers) > 6 else ''}.",
            severity=Severity.ERROR,
            suggestion="Resolve these before releasing the BOM to "
                       "manufacturing.",
        ))
    coverage = float(summary.get("declaration_coverage_pct") or 0)
    if coverage < 80 and summary.get("lines_considered"):
        issues.append(Issue(
            code="compliance_coverage_low",
            message=f"RoHS declarations are only available for "
                    f"{coverage:.0f}% of populated lines.",
            severity=Severity.WARNING,
            suggestion="Request declarations from the manufacturers of the "
                       "remaining parts.",
        ))
    export_parts = summary.get("export_controlled") or []
    if export_parts:
        issues.append(Issue(
            code="compliance_export",
            message=f"{len(export_parts)} line(s) carry export control "
                    f"classifications.",
            severity=Severity.WARNING,
            suggestion="Check destination licence requirements.",
        ))
    return issues
