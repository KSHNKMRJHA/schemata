"""
Match scoring: how confident are we that the catalogue record we found is the
part the BOM meant?

Confidence matters more than it might seem. Every downstream number -- price,
stock, lifecycle, compliance -- is only as trustworthy as the match. So the
engine assigns an explicit 0-100 confidence with written reasons, and flags
anything below the review threshold so a human can approve it before the BOM
goes to purchasing.

Signals used
------------
+ exact normalised MPN equality                 (strongest)
+ manufacturer agreement (alias aware)
+ match after stripping packaging suffixes
+ fuzzy similarity of the raw part numbers
+ agreement of package / value / tolerance / voltage parametrics
- manufacturer contradiction                    (strong negative)
- package contradiction
- value contradiction                           (strong negative)
- provider record flagged as synthesised or unverified
"""

from __future__ import annotations

from typing import Any, Sequence

from ..core.models import (
    BomLine, Completion, Issue, Match, MatchKind, PartData, Severity,
)
from ..util.text import (
    best_ratio, clean, manufacturer_key, mpn_root, normalize_manufacturer,
    normalize_mpn,
)
from ..util.units import (
    normalize_package, packages_equivalent, parse_tolerance, parse_value,
    values_equivalent,
)


def classify(line: BomLine, part: PartData | None,
             fuzzy_threshold: int = 88) -> Match:
    """Compare a BOM line against a candidate part and score the match."""
    match = Match()
    if part is None or not clean(part.mpn):
        match.kind = MatchKind.NONE
        match.confidence = 0
        match.reasons.append("No catalogue record was found for this part.")
        return match

    line_key = normalize_mpn(line.mpn)
    part_key = normalize_mpn(part.mpn)
    line_root = normalize_mpn(mpn_root(line.mpn))
    part_root = normalize_mpn(mpn_root(part.mpn))
    dpn_key = normalize_mpn(line.distributor_pn)

    mfr_line = manufacturer_key(line.manufacturer)
    mfr_part = manufacturer_key(part.manufacturer)
    mfr_agrees = bool(mfr_line and mfr_part and mfr_line == mfr_part)
    mfr_conflicts = bool(mfr_line and mfr_part and mfr_line != mfr_part)

    # -- kind ------------------------------------------------------------- #
    if line_key and line_key == part_key:
        if mfr_agrees:
            match.kind = MatchKind.EXACT_WITH_MFR
            match.reasons.append(
                f"Part number and manufacturer both match "
                f"({part.mpn}, {part.manufacturer}).")
        else:
            match.kind = MatchKind.EXACT
            match.reasons.append(f"Part number matches exactly ({part.mpn}).")
    elif line_root and line_root == part_root:
        match.kind = MatchKind.ROOT
        match.reasons.append(
            f"Matched after ignoring packaging suffixes "
            f"({line.mpn} → {part.mpn}).")
    elif dpn_key and any(normalize_mpn(offer.sku) == dpn_key
                         for offer in part.offers):
        match.kind = MatchKind.DISTRIBUTOR_SKU
        match.reasons.append(
            f"Matched on the distributor part number {line.distributor_pn}.")
    elif line_key:
        similarity = best_ratio(line_key, part_key)
        if similarity >= fuzzy_threshold:
            match.kind = MatchKind.FUZZY
            match.reasons.append(
                f"Part numbers are {similarity:.0f}% similar "
                f"({line.mpn} vs {part.mpn}).")
        else:
            match.kind = MatchKind.NONE
            match.confidence = 0
            match.reasons.append(
                f"The closest catalogue part ({part.mpn}) is only "
                f"{similarity:.0f}% similar — treated as no match.")
            match.needs_review = True
            return match
    else:
        match.kind = MatchKind.DESCRIPTION
        match.reasons.append(
            "No part number on the line; matched from the description.")

    confidence = float(match.kind.base_confidence)

    # -- manufacturer ----------------------------------------------------- #
    if mfr_agrees and match.kind is not MatchKind.EXACT_WITH_MFR:
        confidence += 4
        match.reasons.append("Manufacturer agrees.")
    elif mfr_conflicts:
        confidence -= 28
        match.reasons.append(
            f"Manufacturer differs: the BOM says {line.manufacturer!r}, the "
            f"catalogue says {part.manufacturer!r}.")
    elif mfr_line and not mfr_part:
        confidence -= 2
    elif not mfr_line and mfr_part:
        confidence += 1

    # -- parametric agreement --------------------------------------------- #
    checks: list[tuple[str, bool | None, float, str]] = []

    package_ok = packages_equivalent(line.package, part.package)
    checks.append(("package", package_ok, 6.0,
                   f"footprint {line.package or '?'} vs "
                   f"{part.package or '?'}"))

    value_ok = _value_agrees(line, part)
    checks.append(("value", value_ok, 8.0,
                   f"value {line.value or '?'} vs catalogue parametrics"))

    tolerance_ok = _tolerance_agrees(line, part)
    checks.append(("tolerance", tolerance_ok, 3.0,
                   f"tolerance {line.tolerance or '?'}"))

    voltage_ok = _spec_agrees(line.voltage, part, ("Voltage", "Voltage Rating",
                                                  "Voltage - Rated"))
    checks.append(("voltage", voltage_ok, 3.0,
                   f"voltage {line.voltage or '?'}"))

    for name, outcome, weight, description in checks:
        if outcome is True:
            confidence += weight
            match.reasons.append(f"Confirmed by {description}.")
        elif outcome is False:
            confidence -= weight * 2.2
            match.reasons.append(f"Disagrees on {description}.")

    # -- record quality --------------------------------------------------- #
    record_type = part.specs.get("Record type", "")
    if "Synthesised" in record_type:
        confidence = min(confidence, 40.0)
        match.reasons.append(
            "The only record available is a placeholder from the offline "
            "catalogue.")
    if not part.offers:
        confidence -= 4
        match.reasons.append("No purchasable offers were returned.")

    match.confidence = int(max(0, min(100, round(confidence))))
    match.matched_mpn = part.mpn
    match.matched_manufacturer = part.manufacturer
    match.provider = ", ".join(part.providers)
    return match


def _value_agrees(line: BomLine, part: PartData) -> bool | None:
    """Compare the BOM's Value column against the catalogue parametrics."""
    wanted, kind = parse_value(line.value)
    if wanted is None:
        return None
    candidate_keys = [
        "Resistance", "Capacitance", "Inductance", "Frequency", "Value",
        "Resistance (Ohms)", "Capacitance (F)", "Inductance (H)",
    ]
    for key in candidate_keys:
        if key in part.specs:
            outcome = values_equivalent(line.value, part.specs[key], rel_tol=0.02)
            if outcome is not None:
                return outcome
    # Fall back to the description, which nearly always carries the value.
    for token in part.description.replace("/", " ").split():
        other, other_kind = parse_value(token)
        if other is None or (kind and other_kind and other_kind != kind):
            continue
        if other == 0 or wanted == 0:
            continue
        ratio = max(wanted, other) / min(wanted, other)
        if ratio < 1.03:
            return True
    return None


def _tolerance_agrees(line: BomLine, part: PartData) -> bool | None:
    wanted = parse_tolerance(line.tolerance)
    if wanted is None:
        return None
    for key in ("Tolerance", "Capacitance Tolerance", "Resistance Tolerance"):
        if key in part.specs:
            other = parse_tolerance(part.specs[key])
            if other is not None:
                return abs(other - wanted) < 0.01
    return None


def _spec_agrees(line_value: str, part: PartData,
                 keys: Sequence[str]) -> bool | None:
    wanted, kind = parse_value(line_value)
    if wanted is None:
        return None
    for key in keys:
        for spec_key, spec_value in part.specs.items():
            if key.lower() in spec_key.lower():
                other, other_kind = parse_value(spec_value)
                if other is None:
                    continue
                if kind and other_kind and kind != other_kind:
                    continue
                # A higher rating than requested is fine; lower is not.
                if other >= wanted * 0.99:
                    return True
                return False
    return None


# --------------------------------------------------------------------------- #
# Field completion
# --------------------------------------------------------------------------- #

COMPLETABLE_FIELDS = ("manufacturer", "description", "package", "value",
                      "tolerance", "voltage", "mpn")


def propose_completions(line: BomLine, part: PartData, match: Match,
                        min_confidence: int = 70) -> list[Completion]:
    """Suggest values for blank (or clearly wrong) fields, with provenance.

    Nothing is applied here -- the engine decides whether to auto-apply based
    on the ``auto_apply_completions`` setting, and the UI always shows what
    changed.
    """
    if match.confidence < min_confidence or part is None:
        return []
    source = ", ".join(part.providers) or "catalogue"
    out: list[Completion] = []

    def add(field: str, new_value: str, confidence: int) -> None:
        new_value = clean(new_value)
        old_value = clean(getattr(line, field, ""))
        if not new_value or new_value == old_value:
            return
        out.append(Completion(field=field, old_value=old_value,
                              new_value=new_value, source=source,
                              confidence=confidence))

    if not clean(line.manufacturer):
        add("manufacturer", part.manufacturer, match.confidence)
    elif manufacturer_key(line.manufacturer) != manufacturer_key(
            part.manufacturer) and part.manufacturer:
        # Same company, different spelling -> normalise it.
        if best_ratio(normalize_manufacturer(line.manufacturer),
                      part.manufacturer) >= 82:
            add("manufacturer", part.manufacturer,
                max(60, match.confidence - 10))

    if not clean(line.description):
        add("description", part.description, match.confidence)
    if not clean(line.package) and part.package:
        add("package", part.package, match.confidence)
    elif clean(line.package) and part.package and \
            normalize_package(line.package) != part.package:
        if packages_equivalent(line.package, part.package) is None:
            add("package", part.package, max(55, match.confidence - 20))

    if not clean(line.value):
        for key in ("Resistance", "Capacitance", "Inductance", "Frequency"):
            if key in part.specs:
                add("value", part.specs[key], match.confidence)
                break
    if not clean(line.tolerance) and "Tolerance" in part.specs:
        add("tolerance", part.specs["Tolerance"], match.confidence)
    if not clean(line.voltage):
        for key, value in part.specs.items():
            if "voltage" in key.lower() and "rat" in key.lower():
                add("voltage", value, match.confidence)
                break

    # MPN correction: the catalogue's canonical spelling, when the BOM's differs
    # only cosmetically.
    if clean(line.mpn) and part.mpn and clean(line.mpn) != part.mpn and \
            normalize_mpn(line.mpn) == normalize_mpn(part.mpn):
        add("mpn", part.mpn, match.confidence)
    return out


def apply_completions(line: BomLine, completions: Sequence[Completion],
                      fields: Sequence[str] = COMPLETABLE_FIELDS) -> int:
    """Write accepted completions onto the line. Returns how many applied."""
    applied = 0
    for completion in completions:
        if completion.field not in fields or completion.applied:
            continue
        if hasattr(line, completion.field):
            setattr(line, completion.field, completion.new_value)
            completion.applied = True
            applied += 1
    return applied


def match_issues(line: BomLine, match: Match, review_threshold: int = 90
                 ) -> list[Issue]:
    """Turn a weak or contradictory match into user-facing issues."""
    issues: list[Issue] = []
    if match.kind is MatchKind.NONE:
        if line.has_part_number and not line.dnp:
            issues.append(Issue(
                code="no_catalogue_match",
                message=f"No catalogue data was found for {line.mpn or line.distributor_pn!r}. "
                        f"It cannot be priced, risk-scored or compliance-checked.",
                severity=Severity.ERROR, field="mpn", line_no=line.line_no,
                suggestion="Check the part number for typos, or add the "
                           "manufacturer to narrow the search.",
            ))
        return issues

    if match.confidence < review_threshold:
        match.needs_review = True
        issues.append(Issue(
            code="match_needs_review",
            message=f"Matched to {match.matched_mpn} with "
                    f"{match.confidence}% confidence — please confirm.",
            severity=Severity.WARNING if match.confidence >= 60
            else Severity.ERROR,
            field="mpn", line_no=line.line_no,
            suggestion="; ".join(match.reasons[:3]),
        ))
    if match.kind is MatchKind.ROOT:
        issues.append(Issue(
            code="match_packaging_variant",
            message=f"{line.mpn} was matched to {match.matched_mpn}, which is "
                    f"the same device in different packaging.",
            severity=Severity.INFO, field="mpn", line_no=line.line_no,
            suggestion="Confirm the packaging (tape and reel vs cut tape) "
                       "before ordering.",
        ))
    if match.kind is MatchKind.DESCRIPTION:
        issues.append(Issue(
            code="match_from_description",
            message=f"This line had no part number; {match.matched_mpn} was "
                    f"suggested from the description.",
            severity=Severity.WARNING, field="mpn", line_no=line.line_no,
            suggestion="Add the part number to the BOM to make this "
                       "deterministic.",
        ))
    return issues


def summarise(matches: Sequence[Match]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for match in matches:
        counts[match.kind.value] = counts.get(match.kind.value, 0) + 1
    confidences = [m.confidence for m in matches
                   if m.kind is not MatchKind.NONE]
    return {
        "by_kind": counts,
        "matched": sum(1 for m in matches if m.kind is not MatchKind.NONE),
        "unmatched": sum(1 for m in matches if m.kind is MatchKind.NONE),
        "needs_review": sum(1 for m in matches if m.needs_review),
        "avg_confidence": round(sum(confidences) / len(confidences), 1)
        if confidences else 0.0,
    }
