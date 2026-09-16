"""
Duplicate detection and merging.

Three distinct situations, treated differently:

* **Same part, several lines** -- common when a BOM is exported per-designator
  or per-sub-assembly. Quantities are summed and designators concatenated.
* **Same reference designator on two lines** -- a genuine error: one board
  position cannot hold two parts. Flagged, never silently merged.
* **Near-duplicate part numbers** -- e.g. ``GRM188R71C104KA01D`` and
  ``GRM188R71C104KA01`` (packaging suffix). Reported as a hint.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from ..core.models import BomLine, Issue, Severity
from ..util import log
from ..util.text import clean, manufacturer_key, mpn_root, normalize_mpn

LOG = log.get("ingest.dedupe")


def _merge_key(line: BomLine) -> str | None:
    """Key that identifies "the same part" for merge purposes."""
    mpn_key = normalize_mpn(line.mpn)
    if not mpn_key:
        distributor_key = normalize_mpn(line.distributor_pn)
        if distributor_key:
            return f"DPN:{clean(line.distributor).upper()}:{distributor_key}"
        return None
    mfr = manufacturer_key(line.manufacturer)
    dnp = "DNP" if line.dnp else "FIT"
    return f"MPN:{mpn_key}:{mfr}:{dnp}"


def merge_duplicates(lines: Sequence[BomLine], enabled: bool = True
                     ) -> tuple[list[BomLine], list[Issue]]:
    """Merge lines that describe the same part.

    Returns the new line list (renumbered) plus BOM-level issues describing
    what was merged.
    """
    issues: list[Issue] = []
    if not enabled:
        return list(lines), issues

    groups: dict[str, list[BomLine]] = defaultdict(list)
    order: list[str] = []
    passthrough: list[BomLine] = []

    for line in lines:
        key = _merge_key(line)
        if key is None:
            passthrough.append(line)
            continue
        if key not in groups:
            order.append(key)
        groups[key].append(line)

    merged: list[BomLine] = []
    merged_count = 0
    for key in order:
        members = groups[key]
        if len(members) == 1:
            merged.append(members[0])
            continue
        primary = _pick_primary(members)
        total_qty = sum(member.quantity for member in members)
        refs: list[str] = []
        for member in members:
            for ref in member.ref_designators:
                if ref not in refs:
                    refs.append(ref)
        primary.quantity = total_qty
        primary.ref_designators = refs
        primary.merged_from = [member.line_no for member in members]

        # Union of alternates and notes, keeping the text tidy.
        for member in members:
            if member is primary:
                continue
            for alt in member.alt_mpns:
                if alt.upper() not in {a.upper() for a in primary.alt_mpns}:
                    primary.alt_mpns.append(alt)
            if member.notes and member.notes not in primary.notes:
                primary.notes = "; ".join(
                    part for part in [primary.notes, member.notes] if part)
            for field_name in ("description", "manufacturer", "value",
                               "package", "internal_pn", "tolerance",
                               "voltage", "power"):
                if not getattr(primary, field_name):
                    setattr(primary, field_name, getattr(member, field_name))
            primary.issues.extend(member.issues)

        rows = ", ".join(str(member.source_row) for member in members
                         if member.source_row)
        primary.add_issue(
            "merged_duplicate",
            f"Merged {len(members)} lines for the same part "
            f"(source rows {rows}); quantity totals "
            f"{int(total_qty) if float(total_qty).is_integer() else total_qty}.",
            Severity.INFO,
        )
        merged.append(primary)
        merged_count += len(members) - 1

    result = merged + passthrough
    result.sort(key=lambda line: (line.source_row or 0, line.line_no))
    for index, line in enumerate(result, start=1):
        line.line_no = index

    if merged_count:
        issues.append(Issue(
            code="duplicates_merged",
            message=f"{merged_count} duplicate line(s) were merged into "
                    f"{len(order)} unique part(s).",
            severity=Severity.INFO,
        ))
    return result, issues


def _pick_primary(members: Sequence[BomLine]) -> BomLine:
    """Choose the richest line of a duplicate group to keep."""
    def completeness(line: BomLine) -> tuple[int, int]:
        filled = sum(
            1 for value in (line.mpn, line.manufacturer, line.description,
                            line.package, line.value, line.internal_pn)
            if clean(value)
        )
        return filled, -(line.source_row or 0)

    return max(members, key=completeness)


def find_refdes_conflicts(lines: Sequence[BomLine]) -> list[Issue]:
    """Reference designators used by more than one distinct part."""
    owners: dict[str, list[BomLine]] = defaultdict(list)
    for line in lines:
        for ref in line.ref_designators:
            owners[ref].append(line)

    issues: list[Issue] = []
    reported: set[str] = set()
    for ref, holders in sorted(owners.items()):
        if len(holders) < 2:
            continue
        parts = {normalize_mpn(h.mpn) or f"line{h.line_no}" for h in holders}
        if len(parts) < 2:
            continue
        signature = "|".join(sorted(str(h.line_no) for h in holders))
        if signature in reported:
            continue
        reported.add(signature)
        rows = ", ".join(
            f"line {h.line_no}" + (f" (row {h.source_row})" if h.source_row else "")
            for h in holders
        )
        issue = Issue(
            code="refdes_conflict",
            message=f"Reference designator {ref} is assigned to "
                    f"{len(parts)} different parts: {rows}.",
            severity=Severity.ERROR,
            field="ref_designators",
            suggestion="One board position can only hold one part — check "
                       "the schematic or the export.",
        )
        issues.append(issue)
        for holder in holders:
            holder.issues.append(Issue(**{**issue.to_dict(),
                                          "severity": Severity.ERROR,
                                          "line_no": holder.line_no}))
    return issues


def find_near_duplicates(lines: Sequence[BomLine]) -> list[Issue]:
    """Part numbers that differ only by a packaging suffix or separator."""
    by_root: dict[str, list[BomLine]] = defaultdict(list)
    for line in lines:
        if not line.mpn:
            continue
        root = normalize_mpn(mpn_root(line.mpn))
        if len(root) >= 5:
            by_root[root].append(line)

    issues: list[Issue] = []
    for root, members in sorted(by_root.items()):
        distinct = {normalize_mpn(m.mpn) for m in members}
        if len(members) < 2 or len(distinct) < 2:
            continue
        listing = ", ".join(sorted({m.mpn for m in members}))
        issues.append(Issue(
            code="near_duplicate_mpn",
            message=f"These part numbers differ only by packaging or "
                    f"formatting and may be the same part: {listing}.",
            severity=Severity.WARNING,
            suggestion="Consolidate them if they are the same device, so you "
                       "get the correct price break.",
        ))
        for member in members:
            member.add_issue(
                "near_duplicate_mpn",
                f"Similar part number(s) elsewhere in this BOM: {listing}.",
                Severity.INFO, field="mpn",
            )
    return issues


def find_refdes_quantity_mismatch(lines: Sequence[BomLine]) -> list[Issue]:
    """Quantity that disagrees with the number of reference designators."""
    issues: list[Issue] = []
    for line in lines:
        count = len(line.ref_designators)
        if count == 0 or line.dnp:
            continue
        if abs(line.quantity - count) < 0.001:
            continue
        # A quantity that is an exact multiple often means "per panel".
        if count and line.quantity and line.quantity % count == 0:
            severity = Severity.INFO
            note = (f"Quantity {int(line.quantity)} is {int(line.quantity / count)}"
                    f"× the {count} designator(s) — check whether this "
                    f"is per panel rather than per board.")
        else:
            severity = Severity.WARNING
            note = (f"Quantity {_fmt(line.quantity)} does not match the "
                    f"{count} reference designator(s) listed.")
        line.add_issue("qty_refdes_mismatch", note, severity, field="quantity",
                       suggestion=f"Expected {count} based on the designators.")
        issues.append(Issue(code="qty_refdes_mismatch", message=note,
                            severity=severity, line_no=line.line_no))
    return issues


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
