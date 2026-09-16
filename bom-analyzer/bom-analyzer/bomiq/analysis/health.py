"""
Whole-BOM health score.

One number an engineering manager can act on, built from six components that
are each individually meaningful. The score is 0-100 where *higher is
healthier* (the inverse of the per-line risk score, which is higher-is-worse).

Weighting is by *placement count*, not by line count: a risky part used 40
times on the board hurts more than a risky part used once. That matches how
supply problems actually bite.
"""

from __future__ import annotations

from typing import Sequence

from ..config import Settings
from ..core.models import (
    ComplianceState, HealthScore, Lifecycle, LineResult, MatchKind, RiskLevel,
    Severity,
)

COMPONENT_LABELS = {
    "lifecycle": "Lifecycle health",
    "availability": "Availability",
    "sourcing": "Multi-sourcing",
    "lead_time": "Lead time",
    "compliance": "Compliance",
    "data_quality": "BOM data quality",
}


def _weight(result: LineResult) -> float:
    """Placement weight for one line, floored so every line counts a little."""
    if result.line.dnp:
        return 0.0
    return max(1.0, float(result.line.effective_quantity))


def compute(results: Sequence[LineResult], settings: Settings) -> HealthScore:
    """Roll per-line factors up into a BOM health score."""
    live = [result for result in results if not result.line.dnp]
    if not live:
        return HealthScore(score=0.0, grade="n/a", level=RiskLevel.LOW,
                           headline="This BOM has no populated lines.")

    # Negative or zero weights would break the roll-up, so they are clamped
    # here as well as on the settings write path.
    weights = {
        "lifecycle": max(0.0, settings.weight_lifecycle),
        "availability": max(0.0, settings.weight_availability),
        "sourcing": max(0.0, settings.weight_sourcing),
        "lead_time": max(0.0, settings.weight_lead_time),
        "compliance": max(0.0, settings.weight_compliance),
        "data_quality": max(0.0, settings.weight_data_quality),
    }
    if sum(weights.values()) <= 0.0:
        weights = {key: 1.0 for key in weights}

    # Weighted mean of each risk factor across lines -> component health.
    accumulated: dict[str, float] = {key: 0.0 for key in weights}
    weight_sum = 0.0
    for result in live:
        line_weight = _weight(result)
        weight_sum += line_weight
        by_code = {factor.code: factor for factor in result.risk.factors}
        for key in weights:
            factor = by_code.get(key)
            accumulated[key] += (factor.score if factor else 50.0) * line_weight

    components: dict[str, float] = {}
    for key, total in accumulated.items():
        risk = total / weight_sum if weight_sum else 50.0
        components[key] = round(max(0.0, min(100.0, 100.0 - risk)), 1)

    weight_total = sum(weights.values()) or 1.0
    score = sum(components[key] * weights[key] for key in weights) / weight_total

    # Hard penalties for conditions no average should be allowed to hide.
    penalties: list[tuple[float, str]] = []
    obsolete = [r for r in live if r.part and r.part.lifecycle is
                Lifecycle.OBSOLETE]
    if obsolete:
        penalties.append((min(18.0, 4.0 * len(obsolete)),
                          f"{len(obsolete)} obsolete part(s)"))
    no_stock = [r for r in live if r.part and not r.part.in_stock_offers]
    if no_stock:
        penalties.append((min(15.0, 2.0 * len(no_stock)),
                          f"{len(no_stock)} line(s) with no stock anywhere"))
    unmatched = [r for r in live if r.match.kind is MatchKind.NONE
                 and r.line.has_part_number]
    if unmatched:
        share = len(unmatched) / len(live)
        penalties.append((min(20.0, share * 40.0),
                          f"{len(unmatched)} line(s) with no catalogue data"))
    blockers = [r for r in live
                if r.compliance.rohs is ComplianceState.NON_COMPLIANT
                and settings.require_rohs]
    if blockers:
        penalties.append((min(12.0, 3.0 * len(blockers)),
                          f"{len(blockers)} non-RoHS line(s)"))

    for amount, _ in penalties:
        score -= amount
    score = max(0.0, min(100.0, score))

    health = HealthScore(
        score=round(score, 1),
        grade=grade_for(score),
        level=RiskLevel.from_score(100.0 - score),
        components=components,
        weights={key: round(value, 2) for key, value in weights.items()},
    )
    health.drivers = _drivers(components, penalties)
    health.headline = _headline(health, live, results)
    return health


def grade_for(score: float) -> str:
    if score >= 92:
        return "A+"
    if score >= 85:
        return "A"
    if score >= 78:
        return "B+"
    if score >= 70:
        return "B"
    if score >= 62:
        return "C+"
    if score >= 54:
        return "C"
    if score >= 45:
        return "D"
    return "E"


def _drivers(components: dict[str, float],
             penalties: Sequence[tuple[float, str]]) -> list[str]:
    drivers: list[str] = []
    for amount, label in sorted(penalties, key=lambda item: -item[0]):
        drivers.append(f"{label} (−{amount:.0f} pts)")
    weakest = sorted(components.items(), key=lambda kv: kv[1])[:3]
    for key, value in weakest:
        if value < 80:
            drivers.append(f"{COMPONENT_LABELS.get(key, key)} at {value:.0f}/100")
    return drivers[:6]


def _headline(health: HealthScore, live: Sequence[LineResult],
              all_results: Sequence[LineResult]) -> str:
    critical = sum(1 for r in live if r.risk.level is RiskLevel.CRITICAL)
    high = sum(1 for r in live if r.risk.level is RiskLevel.HIGH)
    errors = sum(1 for r in all_results
                 if any(i.severity is Severity.ERROR for i in r.all_issues))

    if health.score >= 85 and not critical:
        base = "This BOM is in good shape."
    elif health.score >= 70:
        base = "This BOM is usable but has some supply risk."
    elif health.score >= 55:
        base = "This BOM needs attention before release."
    else:
        base = "This BOM carries serious supply risk."

    details: list[str] = []
    if critical:
        details.append(f"{critical} critical line(s)")
    if high:
        details.append(f"{high} high-risk line(s)")
    if errors:
        details.append(f"{errors} line(s) with data errors")
    if details:
        return f"{base} {', '.join(details).capitalize()}."
    return base


def counters(results: Sequence[LineResult]) -> dict[str, dict[str, int]]:
    """Distribution counters used by the dashboard tiles."""
    lifecycle: dict[str, int] = {}
    risk: dict[str, int] = {}
    severity = {"error": 0, "warning": 0, "info": 0}
    compliance = {state.value: 0 for state in ComplianceState}

    for result in results:
        state = result.part.lifecycle.value if result.part else "No data"
        lifecycle[state] = lifecycle.get(state, 0) + 1
        risk[result.risk.level.value] = risk.get(result.risk.level.value, 0) + 1
        worst = result.max_severity.value
        severity[worst] = severity.get(worst, 0) + 1
        compliance[result.compliance.rohs.value] = \
            compliance.get(result.compliance.rohs.value, 0) + 1
    return {
        "lifecycle": lifecycle,
        "risk": risk,
        "severity": severity,
        "compliance_rohs": compliance,
    }


def top_risks(results: Sequence[LineResult], limit: int = 10
              ) -> list[dict[str, object]]:
    """The lines a buyer should look at first."""
    ranked = sorted(
        (r for r in results if not r.line.dnp),
        key=lambda r: (-r.risk.score,
                       -(r.line.effective_quantity or 0)),
    )
    out: list[dict[str, object]] = []
    for result in ranked[:limit]:
        if result.risk.score <= 0:
            continue
        worst = max(result.risk.factors, key=lambda f: f.score, default=None)
        out.append({
            "line_no": result.line.line_no,
            "mpn": result.line.mpn or result.line.internal_pn,
            "manufacturer": result.line.manufacturer,
            "quantity": result.line.quantity,
            "refs": result.line.ref_text,
            "risk_score": result.risk.score,
            "risk_level": result.risk.level.value,
            "driver": worst.label if worst else "",
            "detail": worst.detail if worst else "",
            "flags": list(result.risk.flags),
            "best_alternate": result.alternates[0].mpn
            if result.alternates else "",
        })
    return out
