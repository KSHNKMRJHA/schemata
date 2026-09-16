"""
Risk scoring.

Six factors, each scored 0-100 (higher = worse) and combined with configurable
weights into one per-line risk score. The factors are deliberately independent
and each carries a written explanation, because "this line is risky" is useless
to a buyer -- "this line is risky *because* it is NRND, single-sourced and on a
20-week lead time" is actionable.

Factors
-------
``lifecycle``     EOL / NRND / obsolete status, and whether a replacement exists
``availability``  stock versus the quantity this build needs
``sourcing``      how many authorised distributors actually stock it
``lead_time``     worst-case lead time against the configured thresholds
``compliance``    RoHS / REACH / export-control problems
``data_quality``  match confidence and validation errors on the line
"""

from __future__ import annotations

from typing import Sequence

from ..config import Settings
from ..core.models import (
    ComplianceState, Issue, Lifecycle, LineResult, Match, MatchKind, PartData,
    Risk, RiskFactor, RiskLevel, Severity,
)
from ..util.text import clean
from ..util.units import format_lead_time

# --------------------------------------------------------------------------- #
# Individual factors
# --------------------------------------------------------------------------- #

_LIFECYCLE_SCORE = {
    Lifecycle.ACTIVE: 0.0,
    Lifecycle.NEW: 8.0,
    Lifecycle.UNKNOWN: 35.0,
    Lifecycle.NRND: 65.0,
    Lifecycle.EOL: 88.0,
    Lifecycle.OBSOLETE: 100.0,
}


def lifecycle_factor(part: PartData | None, weight: float) -> RiskFactor:
    if part is None:
        return RiskFactor(
            code="lifecycle", label="Lifecycle", score=55.0, weight=weight,
            detail="No catalogue record, so lifecycle status is unknown.")
    score = _LIFECYCLE_SCORE[part.lifecycle]
    detail = f"{part.lifecycle.value}"
    if part.lifecycle_note and part.lifecycle_note != part.lifecycle.value:
        detail = f"{part.lifecycle.value} — {part.lifecycle_note}"
    if part.lifecycle.is_risky:
        if part.alternate_mpns:
            score -= 8.0
            detail += (f"; the manufacturer or distributor suggests "
                       f"{part.alternate_mpns[0]}")
        else:
            score = min(100.0, score + 4.0)
            detail += "; no replacement has been published"
    if len(part.lifecycle_sources) > 1 and len(
            set(part.lifecycle_sources.values())) > 1:
        detail += (f"; sources disagree ("
                   f"{', '.join(f'{k}: {v}' for k, v in part.lifecycle_sources.items())}"
                   f") — the worst was used")
    return RiskFactor(code="lifecycle", label="Lifecycle",
                      score=max(0.0, min(100.0, score)), weight=weight,
                      detail=detail)


def availability_factor(part: PartData | None, required_qty: float,
                        weight: float, buffer_pct: float = 10.0) -> RiskFactor:
    if part is None:
        return RiskFactor("availability", "Availability", 55.0, weight,
                          "No catalogue record, so stock is unknown.")
    needed = required_qty * (1.0 + max(0.0, buffer_pct) / 100.0)
    stock = part.total_stock
    in_stock_distributors = len({o.distributor for o in part.in_stock_offers})

    if needed <= 0:
        return RiskFactor("availability", "Availability", 0.0, weight,
                          "Nothing to buy for this line.")
    if stock <= 0:
        return RiskFactor("availability", "Availability", 100.0, weight,
                          "No distributor is showing stock.")
    coverage = stock / needed
    if coverage >= 10:
        score = 0.0
    elif coverage >= 3:
        score = 8.0
    elif coverage >= 1.5:
        score = 20.0
    elif coverage >= 1.0:
        score = 38.0
    elif coverage >= 0.5:
        score = 62.0
    else:
        score = 85.0
    # Stock spread across several distributors is safer than one big pile.
    if in_stock_distributors >= 3:
        score *= 0.8
    elif in_stock_distributors == 1:
        score = min(100.0, score + 8.0)
    detail = (f"{stock:,} in stock against {needed:,.0f} needed "
              f"(incl. {buffer_pct:.0f}% buffer) across "
              f"{in_stock_distributors} distributor"
              f"{'s' if in_stock_distributors != 1 else ''}")
    return RiskFactor("availability", "Availability",
                      max(0.0, min(100.0, score)), weight, detail)


def sourcing_factor(part: PartData | None, weight: float,
                    min_sources: int = 2,
                    authorized_only: bool = True) -> RiskFactor:
    if part is None:
        return RiskFactor("sourcing", "Sourcing", 55.0, weight,
                          "No catalogue record, so sourcing is unknown.")
    offers = part.authorized_offers if authorized_only else part.offers
    stocking = {o.distributor for o in offers if o.in_stock}
    listing = {o.distributor for o in offers}
    count = len(stocking)

    if count == 0 and not listing:
        score, detail = 100.0, "No authorised distributor lists this part."
    elif count == 0:
        score = 82.0
        detail = (f"{len(listing)} distributor(s) list it but none have stock.")
    elif count == 1:
        score = 58.0
        detail = f"Single source: only {next(iter(stocking))} has stock."
    elif count < min_sources:
        score = 40.0
        detail = f"{count} stocking source(s), below your target of {min_sources}."
    elif count == min_sources:
        score = 18.0
        detail = f"{count} stocking sources, meeting your target."
    else:
        score = max(0.0, 14.0 - (count - min_sources) * 2.5)
        detail = f"{count} stocking sources — well covered."

    unauthorized = [o for o in part.offers if not o.authorized and o.in_stock]
    if count == 0 and unauthorized:
        # Stock that exists only at brokers is *worse* than no stock listed:
        # it invites an unvetted purchase. It must raise the score, never
        # lower it.
        score = min(100.0, score + 8.0)
        detail += (f" The only stock is at {len(unauthorized)} "
                   f"non-authorised/broker source(s) \u2014 provenance and "
                   f"counterfeit risk apply.")
    return RiskFactor("sourcing", "Sourcing", max(0.0, min(100.0, score)),
                      weight, detail)


def lead_time_factor(part: PartData | None, weight: float,
                     warn_days: int = 84, critical_days: int = 168
                     ) -> RiskFactor:
    if part is None:
        return RiskFactor("lead_time", "Lead time", 45.0, weight,
                          "No catalogue record, so lead time is unknown.")
    if part.in_stock_offers:
        return RiskFactor("lead_time", "Lead time", 0.0, weight,
                          "Available from stock.")
    candidates = [o.lead_time_days for o in part.offers
                  if o.lead_time_days is not None]
    if part.estimated_factory_lead_days:
        candidates.append(part.estimated_factory_lead_days)
    if not candidates:
        return RiskFactor("lead_time", "Lead time", 50.0, weight,
                          "Out of stock and no lead time was published.")
    best = min(candidates)
    if best <= 0:
        return RiskFactor("lead_time", "Lead time", 0.0, weight,
                          "Available from stock.")
    if best >= critical_days:
        score = 100.0
    elif best >= warn_days:
        span = max(1, critical_days - warn_days)
        score = 55.0 + 45.0 * (best - warn_days) / span
    else:
        score = 55.0 * (best / max(1, warn_days))
    return RiskFactor("lead_time", "Lead time", max(0.0, min(100.0, score)),
                      weight,
                      f"Best published lead time is {format_lead_time(best)} "
                      f"({best} days); your warning threshold is "
                      f"{warn_days} days.")


def compliance_factor(part: PartData | None, weight: float,
                      require_rohs: bool = True, require_reach: bool = False,
                      flag_export: bool = True) -> RiskFactor:
    if part is None:
        return RiskFactor("compliance", "Compliance", 40.0, weight,
                          "No catalogue record, so compliance is unknown.")
    compliance = part.compliance
    score = 0.0
    notes: list[str] = []

    if compliance.rohs is ComplianceState.NON_COMPLIANT:
        score += 85.0 if require_rohs else 30.0
        notes.append("not RoHS compliant")
    elif compliance.rohs is ComplianceState.EXEMPT:
        score += 20.0
        notes.append(f"RoHS by exemption ({compliance.rohs_note or 'unstated'})")
    elif compliance.rohs is ComplianceState.UNKNOWN and require_rohs:
        score += 35.0
        notes.append("RoHS status unknown")

    if compliance.reach is ComplianceState.NON_COMPLIANT:
        score += 55.0 if require_reach else 22.0
        notes.append("REACH SVHC declared" +
                     (f" ({', '.join(compliance.svhc[:2])})"
                      if compliance.svhc else ""))
    elif compliance.reach is ComplianceState.UNKNOWN and require_reach:
        score += 28.0
        notes.append("REACH status unknown")

    if flag_export:
        if compliance.itar:
            score += 70.0
            notes.append("ITAR controlled")
        elif compliance.export_controlled:
            score += 35.0
            notes.append(f"export controlled (ECCN {compliance.eccn or '?'})")

    if not notes:
        detail = "RoHS" + (
            " and REACH compliant" if compliance.reach is
            ComplianceState.COMPLIANT else " compliant")
        if compliance.country_of_origin:
            detail += f"; origin {compliance.country_of_origin}"
    else:
        detail = "; ".join(notes).capitalize()
    return RiskFactor("compliance", "Compliance", max(0.0, min(100.0, score)),
                      weight, detail)


def data_quality_factor(match: Match, issues: Sequence[Issue], weight: float,
                        review_threshold: int = 90) -> RiskFactor:
    score = 0.0
    notes: list[str] = []
    if match.kind is MatchKind.NONE:
        score = 100.0
        notes.append("no catalogue match")
    else:
        deficit = max(0, review_threshold - match.confidence)
        score = min(85.0, deficit * 1.6)
        if match.confidence < review_threshold:
            notes.append(f"match confidence {match.confidence}%")
        if match.kind in (MatchKind.FUZZY, MatchKind.DESCRIPTION):
            score = min(100.0, score + 18.0)
            notes.append(f"matched by {match.kind.value}")

    errors = sum(1 for issue in issues if issue.severity is Severity.ERROR)
    warnings = sum(1 for issue in issues if issue.severity is Severity.WARNING)
    if errors:
        score = min(100.0, score + errors * 22.0)
        notes.append(f"{errors} data error(s)")
    if warnings:
        score = min(100.0, score + warnings * 7.0)
        notes.append(f"{warnings} data warning(s)")
    detail = "; ".join(notes).capitalize() if notes else \
        "Part number matched cleanly with no data problems."
    return RiskFactor("data_quality", "Data quality",
                      max(0.0, min(100.0, score)), weight, detail)


def cost_factor(result: LineResult, weight: float) -> RiskFactor:
    """Price volatility / price-availability risk for the line."""
    cost = result.cost
    if cost.unit_price is None:
        return RiskFactor("cost", "Cost certainty", 60.0, weight,
                          "No live price was available for this line.")
    notes: list[str] = []
    score = 0.0
    if cost.price_spread_pct is not None:
        if cost.price_spread_pct > 200:
            score += 55.0
            notes.append(f"prices vary {cost.price_spread_pct:.0f}% between "
                         f"distributors")
        elif cost.price_spread_pct > 80:
            score += 30.0
            notes.append(f"prices vary {cost.price_spread_pct:.0f}%")
        elif cost.price_spread_pct > 30:
            score += 12.0
    if cost.best and cost.best.overbuy_qty > 0:
        required = max(1, cost.required_qty)
        overbuy_share = cost.best.overbuy_qty / required
        if overbuy_share > 2:
            score += 30.0
            notes.append(f"minimum order forces {cost.best.overbuy_qty:,} "
                         f"extra pieces")
        elif overbuy_share > 0.5:
            score += 15.0
            notes.append(f"pack size forces {cost.best.overbuy_qty:,} extra "
                         f"pieces")
    if cost.best and not cost.best.covers_demand:
        score += 40.0
        notes.append("no single distributor can cover the full quantity")
    detail = "; ".join(notes).capitalize() if notes else \
        "Pricing is consistent across distributors."
    return RiskFactor("cost", "Cost certainty", max(0.0, min(100.0, score)),
                      weight, detail)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def score_line(result: LineResult, settings: Settings) -> Risk:
    """Compute the weighted risk for one analysed line."""
    line, part, match = result.line, result.part, result.match
    if line.dnp:
        return Risk(score=0.0, level=RiskLevel.LOW,
                    factors=[RiskFactor("dnp", "Not populated", 0.0, 1.0,
                                        "Do-not-populate lines carry no "
                                        "supply risk.")],
                    flags=["DNP"])

    required = line.effective_quantity * max(1, settings.build_quantity)
    factors = [
        lifecycle_factor(part, settings.weight_lifecycle),
        availability_factor(part, required, settings.weight_availability,
                            settings.stock_buffer_pct),
        sourcing_factor(part, settings.weight_sourcing,
                        settings.min_sources_ok,
                        settings.prefer_authorized_only),
        lead_time_factor(part, settings.weight_lead_time,
                         settings.lead_time_warn_days,
                         settings.lead_time_critical_days),
        compliance_factor(part, settings.weight_compliance,
                          settings.require_rohs, settings.require_reach,
                          settings.flag_export_controlled),
        data_quality_factor(match, result.all_issues,
                            settings.weight_data_quality,
                            settings.review_confidence_threshold),
        cost_factor(result, settings.weight_cost),
    ]

    # Guard the denominator: a misconfigured (or negative) weight set would
    # otherwise divide by ~0 and produce an astronomic score.
    weight_total = sum(max(0.0, f.weight) for f in factors)
    if weight_total <= 0.0:
        weight_total = float(len(factors)) or 1.0
        score = sum(f.score for f in factors) / weight_total
    else:
        score = sum(f.score * max(0.0, f.weight)
                    for f in factors) / weight_total
    score = max(0.0, min(100.0, score))

    # A single catastrophic factor must not be averaged away: obsolete with no
    # stock anywhere is critical regardless of how clean the rest looks.
    worst = max(factors, key=lambda f: f.score)
    if worst.score >= 95 and worst.code in ("lifecycle", "availability",
                                            "sourcing", "compliance"):
        score = max(score, 72.0)
    elif worst.score >= 85 and worst.code in ("lifecycle", "availability"):
        score = max(score, 55.0)

    # Obsolescence is a decision trigger, not an average. A part that is NRND
    # or worse must never read as "Low" just because it happens to be cheap
    # and in stock today -- that is exactly the part that bites in 18 months.
    if part is not None:
        floor = {
            Lifecycle.NRND: 30.0,
            Lifecycle.EOL: 52.0,
            Lifecycle.OBSOLETE: 72.0,
        }.get(part.lifecycle)
        if floor is not None:
            score = max(score, floor)
        elif part.lifecycle is Lifecycle.UNKNOWN and \
                match.kind is not MatchKind.NONE:
            score = max(score, 25.0)
    elif not line.dnp and line.has_part_number:
        # No catalogue record at all: nothing about this line is verified.
        score = max(score, 45.0)

    risk = Risk(score=round(score, 1), level=RiskLevel.from_score(score),
                factors=factors)
    risk.flags = _flags(result, factors, settings)
    return risk


def _flags(result: LineResult, factors: Sequence[RiskFactor],
           settings: Settings) -> list[str]:
    """Short badges shown in the UI grid."""
    flags: list[str] = []
    part = result.part
    if part is not None:
        if part.lifecycle.is_risky:
            flags.append(part.lifecycle.value)
        if not part.authorized_offers and any(
                offer.in_stock for offer in part.offers):
            flags.append("Broker only")
        if part.lifecycle is Lifecycle.UNKNOWN:
            flags.append("Lifecycle unknown")
        if not part.in_stock_offers:
            flags.append("No stock")
        elif len({o.distributor for o in part.in_stock_offers}) == 1:
            flags.append("Single source")
        lead = [o.lead_time_days for o in part.offers
                if o.lead_time_days is not None and o.lead_time_days > 0]
        if not part.in_stock_offers and lead and \
                min(lead) >= settings.lead_time_warn_days:
            flags.append(f"Lead {format_lead_time(min(lead))}")
        compliance = part.compliance
        if compliance.rohs is ComplianceState.NON_COMPLIANT:
            flags.append("Not RoHS")
        elif compliance.rohs is ComplianceState.EXEMPT:
            flags.append("RoHS exempt")
        if compliance.reach is ComplianceState.NON_COMPLIANT:
            flags.append("REACH SVHC")
        if compliance.itar:
            flags.append("ITAR")
        elif compliance.export_controlled:
            flags.append("Export controlled")
    else:
        flags.append("No data")
    if result.match.needs_review:
        flags.append("Review match")
    if any(issue.severity is Severity.ERROR for issue in result.all_issues):
        flags.append("Data error")
    if result.cost.best and result.cost.best.overbuy_qty > 0:
        flags.append("MOQ overbuy")
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for flag in flags:
        if flag not in seen:
            seen.add(flag)
            out.append(flag)
    return out


def risk_issues(result: LineResult, settings: Settings) -> list[Issue]:
    """Convert the highest-scoring risk factors into user-facing issues."""
    issues: list[Issue] = []
    line = result.line
    if line.dnp:
        return issues
    for factor in result.risk.factors:
        if factor.score < 60:
            continue
        severity = Severity.ERROR if factor.score >= 85 else Severity.WARNING
        code = f"risk_{factor.code}"
        suggestion = _suggestion(factor, result)
        issues.append(Issue(
            code=code,
            message=f"{factor.label}: {factor.detail}",
            severity=severity, line_no=line.line_no, suggestion=suggestion,
        ))
    return issues


def _suggestion(factor: RiskFactor, result: LineResult) -> str:
    if factor.code == "lifecycle":
        if result.alternates:
            return (f"Consider {result.alternates[0].mpn} "
                    f"({result.alternates[0].manufacturer}) as a replacement.")
        return "Plan a last-time buy or start a redesign for this part."
    if factor.code == "availability":
        return ("Check the alternates tab, split the order across "
                "distributors, or reduce the build quantity.")
    if factor.code == "sourcing":
        return "Qualify a second source before committing to volume."
    if factor.code == "lead_time":
        return "Place the order early or buy from stock at a premium."
    if factor.code == "compliance":
        return "Confirm with the manufacturer's declaration before shipping."
    if factor.code == "data_quality":
        return "Confirm the match, or correct the BOM line."
    if factor.code == "cost":
        return "Compare the sourcing options for this line."
    return ""
