"""
Alternate-part suggestion and scoring.

Candidates come from four places, in descending order of trust:

1. **The BOM itself** -- alternates the engineer already approved (AML/AVL
   columns). These start with a large bonus: someone has signed them off.
2. **Manufacturer/distributor substitution data** -- DigiKey's substitutions
   endpoint, Mouser's suggested replacement, the manufacturer's published
   replacement for an EOL part.
3. **Aggregator "similar parts"** -- Nexar/Octopart similarity.
4. **Parametric search** -- when a part is obsolete with nothing published, the
   engine searches the catalogue by category and key parameters.

Scoring is explicit and shown to the user: every candidate carries the reasons
it scored well and the concerns that remain, because a "drop-in" that is a
different footprint is worse than useless.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..config import Settings
from ..core.models import (
    Alternate, BomLine, ComplianceState, Lifecycle, PartData,
)
from ..util.money import FxTable, quantize
from ..util.text import clean, manufacturer_key, normalize_mpn, tokens
from ..util.units import (
    mount_type, normalize_package, packages_equivalent, parse_tolerance,
    parse_value, values_equivalent,
)

SOURCE_BONUS = {
    "bom": 26.0,
    "substitution": 18.0,
    "manufacturer": 18.0,
    "similar": 6.0,
    "search": 0.0,
}


def score_alternate(original: PartData | None, line: BomLine,
                    candidate: PartData, source: str,
                    settings: Settings, fx: FxTable,
                    required_qty: int = 0) -> Alternate:
    """Score one candidate substitute out of 100 with written justification."""
    alternate = Alternate(
        mpn=candidate.mpn,
        manufacturer=candidate.manufacturer,
        description=candidate.description,
        lifecycle=candidate.lifecycle,
        source=source,
        url=candidate.product_url,
        stock=candidate.total_stock or None,
    )
    reasons: list[str] = []
    concerns: list[str] = []
    score = 40.0 + SOURCE_BONUS.get(source, 0.0)
    unverified = False

    if source == "bom":
        reasons.append("Already listed as an approved alternate in your BOM.")
    elif source in ("substitution", "manufacturer"):
        reasons.append("Published as a substitute by the manufacturer or "
                       "distributor.")
    elif source == "similar":
        reasons.append("Flagged as a similar part by the aggregator.")
    else:
        reasons.append("Found by parametric search of the catalogue.")

    # -- lifecycle -------------------------------------------------------- #
    if candidate.lifecycle is Lifecycle.ACTIVE:
        score += 16.0
        reasons.append("Active lifecycle status.")
    elif candidate.lifecycle is Lifecycle.NEW:
        score += 12.0
        reasons.append("New product, actively supported.")
    elif candidate.lifecycle is Lifecycle.UNKNOWN:
        score -= 4.0
        concerns.append("Lifecycle status unknown.")
    else:
        score -= 30.0
        concerns.append(f"This alternate is itself {candidate.lifecycle.value}.")
    if original is not None and original.lifecycle.is_risky and \
            candidate.lifecycle is Lifecycle.ACTIVE:
        score += 6.0
        reasons.append(f"Replaces a part that is "
                       f"{original.lifecycle.value.lower()}.")

    # -- availability ----------------------------------------------------- #
    stock = candidate.total_stock
    stocking = len({offer.distributor for offer in candidate.in_stock_offers})
    if stock <= 0:
        score -= 24.0
        concerns.append("No distributor is showing stock.")
    else:
        if required_qty and stock >= required_qty * 2:
            score += 14.0
            reasons.append(f"{stock:,} in stock, comfortably above the "
                           f"{required_qty:,} needed.")
        elif required_qty and stock >= required_qty:
            score += 8.0
            reasons.append(f"{stock:,} in stock, enough for this build.")
        else:
            score += 4.0
            reasons.append(f"{stock:,} in stock.")
        if stocking >= settings.min_sources_ok:
            score += 6.0
            reasons.append(f"Stocked by {stocking} distributors.")
        elif stocking == 1:
            concerns.append("Only one distributor has stock.")

    # -- form, fit, function ---------------------------------------------- #
    package_ok = packages_equivalent(
        line.package or (original.package if original else ""),
        candidate.package)
    if package_ok is True:
        score += 18.0
        alternate.pin_compatible = True
        reasons.append(f"Same footprint ({candidate.package}).")
    elif package_ok is False:
        score -= 34.0
        alternate.pin_compatible = False
        concerns.append(
            f"Different footprint: {normalize_package(line.package) or '?'} "
            f"vs {candidate.package or '?'} — not a drop-in.")
        alternate.spec_deltas.append(
            f"Footprint {normalize_package(line.package) or '?'} → "
            f"{candidate.package or '?'}")
    else:
        concerns.append("Footprint could not be compared.")

    original_mount = mount_type(line.package) or (
        original.mount if original else "")
    if original_mount and candidate.mount and original_mount != candidate.mount:
        score -= 20.0
        concerns.append(f"Mounting differs ({original_mount} vs "
                        f"{candidate.mount}).")

    # -- electrical value ------------------------------------------------- #
    value_outcome = _compare_value(line, original, candidate)
    if value_outcome is True:
        score += 14.0
        reasons.append("Electrical value matches.")
    elif value_outcome is False:
        score -= 40.0
        concerns.append("Electrical value does not match.")
    else:
        concerns.append("Electrical value could not be compared — check "
                        "the datasheet before substituting.")
        unverified = True

    tolerance_outcome = _compare_tolerance(line, original, candidate)
    if tolerance_outcome is True:
        score += 5.0
        reasons.append("Tolerance matches or is tighter.")
    elif tolerance_outcome is False:
        score -= 12.0
        concerns.append("Tolerance is looser than specified.")

    if original is not None and original.category and candidate.category:
        if original.category == candidate.category:
            score += 5.0
        else:
            score -= 10.0
            concerns.append(f"Different category "
                            f"({candidate.category} vs {original.category}).")

    if original is not None and manufacturer_key(original.manufacturer) == \
            manufacturer_key(candidate.manufacturer):
        score += 4.0
        reasons.append("Same manufacturer, so qualification is easier.")

    # -- compliance ------------------------------------------------------- #
    compliance = candidate.compliance
    if compliance.rohs is ComplianceState.NON_COMPLIANT and settings.require_rohs:
        score -= 30.0
        concerns.append("Not RoHS compliant.")
    elif compliance.rohs is ComplianceState.COMPLIANT:
        score += 3.0
    if compliance.reach is ComplianceState.NON_COMPLIANT and settings.require_reach:
        score -= 18.0
        concerns.append("REACH SVHC declared.")
    if compliance.itar or compliance.export_controlled:
        score -= 8.0
        concerns.append("Export controlled.")

    # -- price ------------------------------------------------------------ #
    currency = clean(settings.currency).upper() or "USD"
    candidate_unit = _best_unit_price(candidate, currency, fx)
    original_unit = _best_unit_price(original, currency, fx) if original else None
    alternate.unit_price = candidate_unit
    alternate.currency = currency
    if candidate_unit is not None and original_unit and original_unit > 0:
        delta = float(((candidate_unit - original_unit) / original_unit) * 100)
        alternate.price_delta_pct = round(delta, 1)
        if delta <= -20:
            score += 10.0
            reasons.append(f"{abs(delta):.0f}% cheaper than the original.")
        elif delta <= 5:
            score += 5.0
            reasons.append("Similar price to the original.")
        elif delta <= 40:
            concerns.append(f"{delta:.0f}% more expensive.")
            score -= 5.0
        elif delta <= 100:
            concerns.append(f"{delta:.0f}% more expensive.")
            score -= 14.0
        elif delta <= 300:
            concerns.append(f"{delta:.0f}% more expensive.")
            score -= 22.0
        else:
            concerns.append(f"{delta:.0f}% more expensive — that is a "
                            f"different class of part, not a substitution.")
            score -= 32.0

    # -- ceilings ---------------------------------------------------------- #
    # A candidate whose form/fit or electrical equivalence could not be
    # established must not present as a confident recommendation, however good
    # its stock and price look. The engineer still has to open the datasheet,
    # and the score should say so.
    if value_outcome is False:
        # A different electrical value is not a substitute at any price.
        score = min(score, 30.0)
    if alternate.pin_compatible is False:
        score = min(score, 52.0)
    if unverified and source not in ("bom", "substitution", "manufacturer"):
        score = min(score, 62.0)
    if alternate.pin_compatible is None and source == "search":
        score = min(score, 58.0)

    alternate.score = int(max(0, min(100, round(score))))
    alternate.reasons = reasons
    alternate.concerns = concerns
    return alternate


_VALUE_SPEC_KEYS = ("Resistance", "Capacitance", "Inductance", "Frequency",
                    "Value", "Resistance (Ohms)", "Capacitance (F)")


def _value_from_text(text: str) -> tuple[float | None, str | None]:
    """First parseable engineering value in a free-text description."""
    for token in clean(text).replace("/", " ").replace(",", " ").split():
        value, kind = parse_value(token)
        if value is not None and kind in ("resistance", "capacitance",
                                          "inductance", "frequency"):
            return value, kind
    return None, None


def _compare_value(line: BomLine, original: PartData | None,
                   candidate: PartData) -> bool | None:
    """Compare the original line's electrical value against a candidate.

    Looks for the value in the BOM's own column, then the catalogue
    parametrics, then the description -- because distributor descriptions
    almost always carry it (``CAP CER 0.1UF 16V X7R 0603``) even when the
    parametric block is thin.
    """
    wanted = clean(line.value)
    if not wanted and original is not None:
        for key in _VALUE_SPEC_KEYS:
            if key in original.specs:
                wanted = original.specs[key]
                break
    value, kind = parse_value(wanted) if wanted else (None, None)
    if value is None:
        # Fall back to the descriptions on both sides.
        source_text = (original.description if original else "") or \
            line.description
        value, kind = _value_from_text(source_text)
        if value is None:
            return None
        other, other_kind = _value_from_text(candidate.description)
        if other is None:
            return None
        if kind and other_kind and kind != other_kind:
            return False
        if other == 0 or value == 0:
            return other == value
        return max(value, other) / min(value, other) < 1.03

    for key in _VALUE_SPEC_KEYS:
        if key in candidate.specs:
            outcome = values_equivalent(wanted, candidate.specs[key],
                                        rel_tol=0.02)
            if outcome is not None:
                return outcome
    other, other_kind = _value_from_text(candidate.description)
    if other is None or other == 0:
        return None
    if kind and other_kind and kind != other_kind:
        return False
    return max(value, other) / min(value, other) < 1.03


def _compare_tolerance(line: BomLine, original: PartData | None,
                       candidate: PartData) -> bool | None:
    wanted = parse_tolerance(line.tolerance)
    if wanted is None and original is not None:
        wanted = parse_tolerance(original.specs.get("Tolerance", ""))
    if wanted is None:
        return None
    other = parse_tolerance(candidate.specs.get("Tolerance", ""))
    if other is None:
        return None
    return other <= wanted + 0.001


def _best_unit_price(part: PartData | None, currency: str,
                     fx: FxTable) -> Decimal | None:
    if part is None:
        return None
    prices: list[Decimal] = []
    for offer in part.offers:
        price = offer.min_unit_price
        if price is None:
            continue
        converted = fx.convert(price, offer.currency, currency)
        if converted is not None:
            prices.append(converted)
    if prices:
        return quantize(min(prices), Decimal("0.000001"))
    if part.median_price_1k:
        return quantize(
            fx.convert(part.median_price_1k,
                       part.median_price_1k_currency or "USD", currency),
            Decimal("0.000001"))
    return None


# --------------------------------------------------------------------------- #
# Candidate gathering
# --------------------------------------------------------------------------- #

def needs_alternates(part: PartData | None, required_qty: int,
                     settings: Settings) -> bool:
    """Only spend API calls looking for alternates where they would help."""
    if part is None:
        return False
    if part.lifecycle.is_risky or part.lifecycle is Lifecycle.UNKNOWN:
        return True
    if not part.in_stock_offers:
        return True
    if part.total_stock < required_qty:
        return True
    if len({o.distributor for o in part.in_stock_offers}) < settings.min_sources_ok:
        return True
    if part.compliance.rohs is ComplianceState.NON_COMPLIANT and \
            settings.require_rohs:
        return True
    return False


def parametric_query(line: BomLine, part: PartData | None) -> str:
    """Build a keyword query describing the part, for last-resort searching."""
    pieces: list[str] = []
    if part is not None:
        if part.category:
            pieces.append(part.category)
        for key in ("Resistance", "Capacitance", "Inductance", "Frequency",
                    "Tolerance", "Voltage - Rated", "Power (Watts)"):
            if key in part.specs:
                pieces.append(part.specs[key])
        if part.package:
            pieces.append(part.package)
    if line.value:
        pieces.append(line.value)
    if line.package:
        pieces.append(normalize_package(line.package))
    if line.tolerance:
        pieces.append(line.tolerance)
    if not pieces and line.description:
        pieces.extend(tokens(line.description)[:6])
    seen: set[str] = set()
    out: list[str] = []
    for piece in pieces:
        text = clean(piece)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return " ".join(out[:8])


def build_alternates(line: BomLine, original: PartData | None,
                     candidates: Sequence[tuple[PartData, str]],
                     settings: Settings, fx: FxTable,
                     required_qty: int = 0) -> list[Alternate]:
    """Score, de-duplicate and rank every candidate for one line."""
    best_by_mpn: dict[str, Alternate] = {}
    original_key = normalize_mpn(original.mpn) if original else \
        normalize_mpn(line.mpn)

    for candidate, source in candidates:
        if candidate is None or not clean(candidate.mpn):
            continue
        key = normalize_mpn(candidate.mpn)
        if not key or key == original_key:
            continue
        scored = score_alternate(original, line, candidate, source, settings,
                                 fx, required_qty)
        existing = best_by_mpn.get(key)
        if existing is None or scored.score > existing.score:
            best_by_mpn[key] = scored

    ranked = sorted(best_by_mpn.values(),
                    key=lambda item: (-item.score, item.mpn))
    return ranked[:max(1, settings.max_alternates_per_line)]


def describe_best(alternates: Sequence[Alternate]) -> str:
    """One-line summary for the report's action column."""
    if not alternates:
        return ""
    best = alternates[0]
    verdict = "drop-in" if best.pin_compatible else (
        "needs review" if best.pin_compatible is False else "likely fit")
    return (f"{best.mpn} ({best.manufacturer}) — {best.score}/100, "
            f"{verdict}")
