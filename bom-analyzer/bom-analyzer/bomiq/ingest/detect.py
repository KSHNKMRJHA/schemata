"""
Table region detection: find the header row and the data block inside a sheet
that may also contain title blocks, logos, revision tables, notes and totals.

Real BOMs are messy. This module handles:

* preamble metadata rows (``Project: Foo``, ``Rev: C``, ``Date: ...``)
* multi-row / stacked headers ("Manufacturer" over "Part Number")
* merged-cell headers that arrive as blanks
* sheets where the table starts at column D
* trailing total / signature / note rows
* section banner rows ("--- RESISTORS ---") inside the data
* sheet selection when a workbook has several tabs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from ..util import log
from ..util.text import clean, header_key, tokens
from .mapping import _SYNONYM_INDEX, FIELD_BY_KEY, REQUIRED_FIELDS
from .readers import RawSheet

LOG = log.get("ingest.detect")


_METADATA_LABEL_RE = re.compile(
    r"^(project|program|product|assembly|assy|board|pcb|bom|part|drawing|dwg|"
    r"revision|rev|version|date|prepared|approved|checked|author|engineer|"
    r"customer|client|company|department|title|description|notes?|file|"
    r"issue|status|page|sheet|quantity per|build|variant|configuration|"
    r"document|doc|ecn|eco|released?|owner|created|modified|"
    r"stückliste|zeichnung)\b\s*[:\-]?\s*$",
    re.IGNORECASE,
)

_TOTAL_ROW_RE = re.compile(
    r"^(total|totals|grand total|sub[- ]?total|sum|subtotal|end of bom|"
    r"end of list|notes?|legend|approved by|prepared by|checked by|"
    r"signature|remarks?)\b",
    re.IGNORECASE,
)

_SECTION_BANNER_RE = re.compile(
    r"^[\s\-=*_#]*((resistors?|capacitors?|inductors?|connectors?|"
    r"semiconductors?|ics?|integrated circuits?|diodes?|transistors?|"
    r"crystals?|oscillators?|leds?|switches?|fuses?|relays?|transformers?|"
    r"mechanical|hardware|pcb|misc(ellaneous)?|passives?|actives?|"
    r"electromechanical|section|group|category|smt|tht|assembly)"
    r"[\s\-=*_#:]*)$",
    re.IGNORECASE,
)

_SHEET_NAME_PENALTY = re.compile(
    r"(instruction|readme|help|legend|notes?|history|revision|change|"
    r"cover|title|template|pivot|chart|graph|summary|dashboard|"
    r"do not|obsolete|old|backup|copy of)",
    re.IGNORECASE,
)

_SHEET_NAME_BONUS = re.compile(
    r"(bom|bill of material|parts?|component|material|stückliste|"
    r"nomenclature|lista)", re.IGNORECASE,
)


@dataclass
class TableRegion:
    """Where the tabular data lives inside a sheet."""

    sheet: RawSheet
    header_row: int                     # 0-based index into sheet.rows
    data_start: int                     # 0-based index of the first data row
    data_end: int                       # exclusive
    first_col: int = 0
    last_col: int = 0                   # exclusive
    headers: list[str] = field(default_factory=list)
    score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stacked_header_rows: int = 1
    skipped_rows: list[int] = field(default_factory=list)
    #: non-empty source rows that were deliberately excluded, so the pipeline
    #: can tell the user instead of losing them silently
    dropped_rows: int = 0

    @property
    def row_count(self) -> int:
        return max(0, self.data_end - self.data_start)

    def data_rows(self) -> list[tuple[int, list[str]]]:
        """``[(source_row_number_1_based, cells)]`` for the data block."""
        out: list[tuple[int, list[str]]] = []
        width = self.last_col - self.first_col
        for index in range(self.data_start, min(self.data_end, len(self.sheet.rows))):
            if index in self.skipped_rows:
                continue
            row = self.sheet.rows[index]
            window = row[self.first_col:self.last_col]
            if len(window) < width:
                window = list(window) + [""] * (width - len(window))
            out.append((index + 1 + self.sheet.row_offset, window))
        return out

    def column_samples(self, limit: int = 60) -> list[list[str]]:
        """Per-column sample values, for content-based mapping."""
        width = self.last_col - self.first_col
        columns: list[list[str]] = [[] for _ in range(width)]
        for _, row in self.data_rows()[:limit]:
            for index in range(width):
                value = clean(row[index]) if index < len(row) else ""
                if value:
                    columns[index].append(value)
        return columns


# --------------------------------------------------------------------------- #
# Header row scoring
# --------------------------------------------------------------------------- #

def score_header_row(cells: Sequence[str]) -> tuple[float, int]:
    """Score how strongly a row looks like a header. Returns ``(score, hits)``."""
    values = [clean(cell) for cell in cells]
    filled = [value for value in values if value]
    if len(filled) < 2:
        return 0.0, 0

    hits = 0
    required_hits = 0
    for value in filled:
        key = header_key(value)
        if not key:
            continue
        match = _SYNONYM_INDEX.get(key)
        if match is None:
            for spec in FIELD_BY_KEY.values():
                if any(re.search(pattern, key) for pattern in spec.patterns):
                    match = (spec.key, 70.0)
                    break
        if match:
            hits += 1
            if match[0] in REQUIRED_FIELDS:
                required_hits += 1

    fill_ratio = len(filled) / max(1, len(values))
    hit_ratio = hits / max(1, len(filled))
    # Headers are short, textual and unique.
    numeric = sum(1 for value in filled
                  if re.fullmatch(r"-?[\d.,]+", value)) / len(filled)
    long_cells = sum(1 for value in filled if len(value) > 48) / len(filled)
    unique_ratio = len({header_key(v) for v in filled}) / len(filled)

    score = (
        hits * 12.0
        + required_hits * 10.0
        + hit_ratio * 25.0
        + fill_ratio * 12.0
        + unique_ratio * 10.0
        - numeric * 40.0
        - long_cells * 25.0
    )
    if required_hits >= 2:
        score += 18.0
    return max(0.0, score), hits


def _is_metadata_row(cells: Sequence[str]) -> tuple[bool, tuple[str, str] | None]:
    """Detect ``Label: value`` preamble rows and extract the pair."""
    values = [clean(cell) for cell in cells]
    filled = [(index, value) for index, value in enumerate(values) if value]
    if not filled or len(filled) > 6:
        return False, None
    first_index, first = filled[0]
    label = first.rstrip(":").strip()
    if _METADATA_LABEL_RE.match(first) or _METADATA_LABEL_RE.match(label + " "):
        value = ""
        for index, candidate in filled[1:]:
            if index > first_index:
                value = candidate
                break
        if ":" in first and not value:
            parts = first.split(":", 1)
            label, value = parts[0].strip(), parts[1].strip()
        return True, (label, value)
    if ":" in first and len(filled) <= 2:
        label, value = first.split(":", 1)
        if _METADATA_LABEL_RE.match(label.strip() + " "):
            if not value.strip() and len(filled) > 1:
                value = filled[1][1]
            return True, (label.strip(), value.strip())
    return False, None


def _looks_like_banner(cells: Sequence[str]) -> bool:
    filled = [clean(cell) for cell in cells if clean(cell)]
    if len(filled) != 1:
        return False
    return bool(_SECTION_BANNER_RE.match(filled[0])) or \
        bool(re.fullmatch(r"[\-=*_\s#]{3,}", filled[0]))


def _looks_like_total(cells: Sequence[str]) -> bool:
    filled = [clean(cell) for cell in cells if clean(cell)]
    if not filled:
        return False
    return bool(_TOTAL_ROW_RE.match(filled[0])) and len(filled) <= 5


# --------------------------------------------------------------------------- #
# Column window detection
# --------------------------------------------------------------------------- #

def _column_window(rows: Sequence[Sequence[str]], header_index: int
                   ) -> tuple[int, int]:
    """Find the contiguous column span the table actually occupies."""
    header = rows[header_index]
    filled = [index for index, cell in enumerate(header) if clean(cell)]
    if not filled:
        return 0, max((len(row) for row in rows), default=0)
    first, last = filled[0], filled[-1] + 1
    # Extend right if data rows are wider and the header was merged.
    data_width = max(
        (len(row) for row in rows[header_index + 1:header_index + 30]),
        default=last,
    )
    if data_width > last:
        gap_only = all(
            not clean(cell)
            for row in rows[header_index + 1:header_index + 30]
            for cell in row[last:data_width]
        )
        if not gap_only:
            last = data_width
    return first, last


def _merge_stacked_header(rows: Sequence[Sequence[str]], header_index: int,
                          first_col: int, last_col: int
                          ) -> tuple[list[str], int]:
    """Combine a header split over two or three rows into one label per column.

    Returns ``(headers, rows_consumed)``.
    """
    width = last_col - first_col

    def window(index: int) -> list[str]:
        if index < 0 or index >= len(rows):
            return [""] * width
        row = list(rows[index])[first_col:last_col]
        return [clean(cell) for cell in row] + [""] * (width - len(row))

    base = window(header_index)
    consumed = 1
    candidates: list[list[str]] = []

    # Look one row above and below for complementary fragments.
    above = window(header_index - 1)
    below = window(header_index + 1)

    def complements(other: list[str]) -> bool:
        """True when ``other`` is a fragment of the same header, not data.

        Two accepted shapes:

        * it fills blanks in the base row (a group label above sub-labels, or a
          merged cell that arrived as a gap), and fills more blanks than it
          overlaps;
        * it is a short, wordy, header-ish row sitting directly over/under the
          base row (``Manufacturer`` above ``Part Number``).

        A data row must never qualify, so the second shape demands both few
        filled cells and recognisable header keywords.
        """
        if not any(other):
            return False
        filled_other = sum(1 for cell in other if cell)
        filled_base = sum(1 for cell in base if cell)
        if filled_base == 0 or filled_other > filled_base:
            return False
        overlap = sum(1 for a, b in zip(base, other) if a and b)
        adds = sum(1 for a, b in zip(base, other) if not a and b)
        if adds >= 1 and adds >= overlap:
            return True
        if overlap and 2 <= filled_other <= max(2, filled_base // 2):
            header_score, hits = score_header_row(other)
            numeric = sum(1 for cell in other
                          if cell and re.fullmatch(r"-?[\d.,]+", cell))
            return hits >= 1 and header_score >= 20 and numeric == 0
        return False

    if complements(above):
        candidates.append(above)
    if complements(below):
        candidates.append(below)
        consumed = 2

    if not candidates:
        return base, 1

    merged: list[str] = []
    for index in range(width):
        parts = [base[index]]
        for candidate in candidates:
            piece = candidate[index]
            if piece and piece.lower() not in " ".join(parts).lower():
                parts.append(piece)
        merged.append(" ".join(part for part in parts if part).strip())

    # Forward-fill for merged cells: an empty header inherits the one to its
    # left only when the left label is a group label (e.g. "Price" over
    # "1k | 10k"). We keep it simple and only fill from the row above.
    return merged, consumed


# --------------------------------------------------------------------------- #
# Public detection
# --------------------------------------------------------------------------- #

def detect_region(sheet: RawSheet, scan_rows: int = 40) -> TableRegion | None:
    """Locate the header row and data block in one sheet."""
    rows = sheet.rectangular()
    if not rows:
        return None

    metadata: dict[str, str] = {}
    best_index, best_score, best_hits = -1, 0.0, 0
    limit = min(len(rows), max(scan_rows, 10))

    for index in range(limit):
        score, hits = score_header_row(rows[index])
        # Prefer earlier rows when scores tie.
        score -= index * 0.35
        if score > best_score:
            best_index, best_score, best_hits = index, score, hits

    if best_index < 0 or best_hits == 0:
        # No recognisable header: fall back to the first row that has at least
        # two filled cells and treat it as a header anyway. The mapper's
        # content inference can still rescue the file.
        for index in range(min(len(rows), 10)):
            if sum(1 for cell in rows[index] if clean(cell)) >= 2:
                best_index, best_score = index, 1.0
                break
        if best_index < 0:
            return None
        LOG.info("Sheet %r: no header keywords found; assuming row %d.",
                 sheet.name, best_index + 1)

    # Harvest preamble metadata.
    for index in range(best_index):
        is_meta, pair = _is_metadata_row(rows[index])
        if is_meta and pair and pair[0]:
            metadata.setdefault(pair[0], pair[1])

    first_col, last_col = _column_window(rows, best_index)
    headers, consumed = _merge_stacked_header(rows, best_index, first_col, last_col)
    data_start = best_index + consumed

    # Walk the data block, skipping banner and total/notes rows rather than
    # truncating at them. Truncating is how BOM lines silently disappear, so
    # the table only ends where genuinely nothing meaningful follows, and
    # anything dropped is counted and reported.
    data_end = len(rows)
    skipped: list[int] = []
    dropped_rows = 0
    blank_run = 0
    for index in range(data_start, len(rows)):
        row = rows[index][first_col:last_col]
        if not any(clean(cell) for cell in row):
            blank_run += 1
            skipped.append(index)
            continue
        blank_run = 0
        if _looks_like_banner(row):
            skipped.append(index)
            continue
        if _looks_like_total(row):
            # A total or notes row usually ends the table, but plenty of files
            # put notes in the middle. Only stop when nothing substantial
            # follows it at all.
            meaningful = sum(
                1 for r in rows[index + 1:]
                if sum(1 for cell in r[first_col:last_col] if clean(cell)) >= 3
            )
            if meaningful == 0:
                data_end = index
                dropped_rows = sum(
                    1 for r in rows[index + 1:]
                    if any(clean(cell) for cell in r[first_col:last_col]))
                break
            skipped.append(index)
    else:
        if blank_run:
            data_end = len(rows) - blank_run

    region_notes: list[str] = []
    if dropped_rows:
        region_notes.append(
            f"Stopped at a total/notes row on source row {data_end + 1}; "
            f"{dropped_rows} further non-empty row(s) below it were ignored.")

    region = TableRegion(
        sheet=sheet, header_row=best_index, data_start=data_start,
        data_end=data_end, first_col=first_col, last_col=last_col,
        headers=headers, score=best_score, metadata=metadata,
        stacked_header_rows=consumed,
        skipped_rows=[i for i in skipped if data_start <= i < data_end],
    )
    region.notes.extend(region_notes)
    region.dropped_rows = dropped_rows
    if consumed > 1:
        region.notes.append(
            f"Header spans {consumed} rows; labels were combined.")
    if first_col > 0:
        region.notes.append(
            f"Table starts at spreadsheet column {_col_name(first_col)}.")
    if metadata:
        region.notes.append(
            f"Read {len(metadata)} metadata field(s) from the title block.")
    return region


def _col_name(index: int) -> str:
    name = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def pick_best_region(sheets: Sequence[RawSheet], scan_rows: int = 40,
                     multi_sheet: bool = True) -> tuple[TableRegion | None,
                                                        list[TableRegion]]:
    """Choose the sheet most likely to hold the BOM.

    Returns ``(best, all_candidates)``. When ``multi_sheet`` is true and several
    sheets share the same header layout (a BOM split per assembly), the caller
    can concatenate them -- see :func:`compatible_regions`.
    """
    candidates: list[TableRegion] = []
    for sheet in sheets:
        region = detect_region(sheet, scan_rows=scan_rows)
        if region is None or region.row_count == 0:
            continue
        bonus = 0.0
        if _SHEET_NAME_BONUS.search(sheet.name):
            bonus += 20.0
        if _SHEET_NAME_PENALTY.search(sheet.name):
            bonus -= 30.0
        # Bigger tables win, with diminishing returns.
        bonus += min(30.0, region.row_count ** 0.5 * 2.0)
        bonus += min(12.0, (region.last_col - region.first_col) * 0.6)
        region.score += bonus
        candidates.append(region)

    if not candidates:
        return None, []
    candidates.sort(key=lambda r: -r.score)
    return candidates[0], candidates


def compatible_regions(best: TableRegion, candidates: Sequence[TableRegion],
                       min_similarity: float = 0.8) -> list[TableRegion]:
    """Other sheets whose header layout matches ``best`` closely enough to
    append (multi-sheet BOMs, one tab per sub-assembly)."""
    reference = {header_key(h) for h in best.headers if header_key(h)}
    if not reference:
        return [best]
    out = [best]
    for region in candidates:
        if region is best:
            continue
        keys = {header_key(h) for h in region.headers if header_key(h)}
        if not keys:
            continue
        overlap = len(reference & keys) / len(reference | keys)
        if overlap >= min_similarity:
            out.append(region)
    return out


def summarise_region(region: TableRegion) -> dict[str, object]:
    return {
        "sheet": region.sheet.name,
        "header_row": region.header_row + 1,
        "data_rows": region.row_count,
        "first_column": _col_name(region.first_col),
        "columns": len(region.headers),
        "headers": list(region.headers),
        "metadata": dict(region.metadata),
        "notes": list(region.notes),
        "score": round(region.score, 1),
    }


def preview(region: TableRegion, rows: int = 8) -> dict[str, object]:
    """Header plus a few data rows, for the UI's mapping screen."""
    data = region.data_rows()[:rows]
    return {
        "headers": list(region.headers),
        "rows": [
            {"source_row": source_row, "cells": [clean(c) for c in cells]}
            for source_row, cells in data
        ],
        "total_rows": region.row_count,
    }


def all_tokens_in_region(region: TableRegion, limit: int = 400) -> set[str]:
    """Token bag used for heuristics like detecting units rows."""
    bag: set[str] = set()
    for _, row in region.data_rows()[:limit]:
        for cell in row:
            bag.update(tokens(cell))
    return bag
