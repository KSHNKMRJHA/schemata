"""
Ingestion pipeline: file (or bytes) in, validated :class:`Bom` out.

    read -> pick sheet(s) -> detect header -> map columns -> normalise rows
         -> merge duplicates -> validate

Every stage records what it did, so the UI can explain the result and the user
can intervene at any point (choose another sheet, override the header row,
remap a column) and re-run only the stages that follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..core.errors import FormatError, MappingError
from ..core.models import Bom, Issue, Severity
from ..util import log
from ..util.text import clean
from . import dedupe, validate as validate_mod
from .detect import (
    TableRegion, compatible_regions, detect_region, pick_best_region, preview,
    summarise_region,
)
from .mapping import ColumnMapper, MappingResult, REQUIRED_FIELDS
from .normalize import normalize_rows, summarise_lines
from .readers import RawSheet, read_bytes, read_file

LOG = log.get("ingest.pipeline")


@dataclass
class IngestOptions:
    multi_sheet: bool = True
    combine_matching_sheets: bool = True
    header_scan_rows: int = 40
    expand_ref_ranges: bool = True
    treat_blank_qty_as_one: bool = True
    merge_duplicate_mpns: bool = True
    learn_templates: bool = True
    sheet_name: str | None = None          # force a sheet
    header_row: int | None = None          # force a header row (1-based)
    forced_mapping: dict[str, int] = field(default_factory=dict)
    disabled_rules: tuple[str, ...] = ()
    build_quantity: int = 1

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> "IngestOptions":
        options = cls(
            multi_sheet=getattr(settings, "multi_sheet", True),
            header_scan_rows=getattr(settings, "header_scan_rows", 40),
            expand_ref_ranges=getattr(settings, "expand_ref_ranges", True),
            treat_blank_qty_as_one=getattr(settings, "treat_blank_qty_as_one",
                                           True),
            merge_duplicate_mpns=getattr(settings, "merge_duplicate_mpns", True),
            learn_templates=getattr(settings, "learn_templates", True),
            build_quantity=getattr(settings, "build_quantity", 1),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(options, key):
                setattr(options, key, value)
        return options


@dataclass
class IngestResult:
    bom: Bom
    mapping: MappingResult
    regions: list[TableRegion] = field(default_factory=list)
    chosen: TableRegion | None = None
    sheets: list[RawSheet] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bom": self.bom.to_dict(),
            "mapping": self.mapping.to_dict(),
            "chosen_region": summarise_region(self.chosen) if self.chosen else None,
            "available_sheets": [
                {
                    "name": region.sheet.name,
                    "rows": region.row_count,
                    "columns": len(region.headers),
                    "header_row": region.header_row + 1,
                    "score": round(region.score, 1),
                }
                for region in self.regions
            ],
            "preview": preview(self.chosen) if self.chosen else None,
            "stats": dict(self.stats),
            "notes": list(self.notes),
        }


class Ingestor:
    """Runs the ingestion pipeline. Reusable and stateless between calls."""

    def __init__(self, template_store: Any | None = None) -> None:
        self.template_store = template_store

    # -- entry points ----------------------------------------------------- #

    def ingest_file(self, path: str | Path,
                    options: IngestOptions | None = None) -> IngestResult:
        path = Path(path)
        options = options or IngestOptions()
        sheets = read_file(path, multi_sheet=options.multi_sheet)
        return self._ingest_sheets(sheets, options, source_file=str(path),
                                   name=path.stem)

    def ingest_bytes(self, raw: bytes, name: str = "upload",
                     options: IngestOptions | None = None) -> IngestResult:
        options = options or IngestOptions()
        sheets = read_bytes(raw, name=name, multi_sheet=options.multi_sheet)
        return self._ingest_sheets(sheets, options, source_file=name,
                                   name=Path(name).stem)

    # -- core ------------------------------------------------------------- #

    def _ingest_sheets(self, sheets: Sequence[RawSheet], options: IngestOptions,
                       source_file: str, name: str) -> IngestResult:
        if not sheets:
            raise FormatError("Nothing readable was found in this file.")

        notes: list[str] = []
        for sheet in sheets:
            notes.extend(sheet.notes)

        # 1) choose the table region
        if options.sheet_name:
            wanted = [s for s in sheets
                      if clean(s.name).lower() == clean(options.sheet_name).lower()]
            if not wanted:
                raise FormatError(
                    f"Sheet {options.sheet_name!r} was not found. Available: "
                    f"{', '.join(s.name for s in sheets)}.")
            chosen = detect_region(wanted[0], scan_rows=options.header_scan_rows)
            _, regions = pick_best_region(sheets,
                                          scan_rows=options.header_scan_rows)
            if chosen is None:
                raise FormatError(
                    f"No table was found on sheet {options.sheet_name!r}.")
        else:
            chosen, regions = pick_best_region(
                sheets, scan_rows=options.header_scan_rows,
                multi_sheet=options.multi_sheet)
            if chosen is None:
                raise FormatError(
                    "No table with a recognisable header row was found. "
                    "If the BOM starts partway down the sheet, try selecting "
                    "the sheet and header row manually.")

        # 2) honour a forced header row
        if options.header_row:
            index = max(0, int(options.header_row) - 1)
            forced = detect_region(chosen.sheet,
                                   scan_rows=options.header_scan_rows)
            if forced is not None:
                rows = chosen.sheet.rectangular()
                if index < len(rows):
                    chosen.header_row = index
                    chosen.headers = [clean(c) for c in rows[index]
                                      [chosen.first_col:chosen.last_col]]
                    chosen.data_start = index + 1
                    chosen.stacked_header_rows = 1
                    notes.append(f"Header row forced to row {index + 1}.")

        regions_to_read = [chosen]
        if options.combine_matching_sheets and options.multi_sheet and \
                not options.sheet_name:
            regions_to_read = compatible_regions(chosen, regions)
            if len(regions_to_read) > 1:
                names = ", ".join(r.sheet.name for r in regions_to_read)
                notes.append(
                    f"{len(regions_to_read)} sheets share the same layout and "
                    f"were combined: {names}.")

        # 3) map the columns
        mapper = ColumnMapper(self.template_store)
        mapping = mapper.map_columns(
            chosen.headers, samples=chosen.column_samples(),
            forced=options.forced_mapping or None,
        )

        if mapping.missing_required:
            # Try the runner-up sheets before giving up: a cover sheet can
            # out-score the real BOM when it has a keyword-rich title block.
            for candidate in regions:
                if candidate is chosen:
                    continue
                trial = mapper.map_columns(
                    candidate.headers, samples=candidate.column_samples())
                if not trial.missing_required:
                    LOG.info("Switching to sheet %r which has the required "
                             "columns.", candidate.sheet.name)
                    notes.append(
                        f"Sheet {chosen.sheet.name!r} lacked required columns; "
                        f"used {candidate.sheet.name!r} instead.")
                    chosen, mapping = candidate, trial
                    regions_to_read = [candidate]
                    break

        if "mpn" not in mapping.mapping and "internal_pn" in mapping.mapping:
            # BOMs that only carry an internal part number: use it as the key
            # so the rest of the pipeline still works.
            mapping.mapping["mpn"] = mapping.mapping["internal_pn"]
            mapping.confidence["mpn"] = 45.0
            mapping.reasons["mpn"] = (
                "no manufacturer part number column; using the internal part "
                "number as the lookup key")
            notes.append("No MPN column was found; the internal part number "
                         "column is being used for lookups.")

        if mapping.missing_required:
            missing = ", ".join(
                key.replace("_", " ") for key in mapping.missing_required)
            raise MappingError(
                f"Could not identify the required column(s): {missing}. "
                f"Detected headers: "
                f"{', '.join(h for h in mapping.headers if h) or '(none)'}. "
                f"Use the column mapping panel to set them manually."
            )

        # 4) normalise rows
        bom = Bom(
            name=name or "BOM",
            source_file=source_file,
            source_format=chosen.sheet.source_format,
            header_row=chosen.header_row + 1,
            column_map={
                key: mapping.headers[index]
                for key, index in mapping.mapping.items()
                if index < len(mapping.headers)
            },
            unmapped_columns=[
                mapping.headers[index] for index in mapping.unmapped_indices
                if index < len(mapping.headers)
            ],
            template_name=mapping.template_name,
            template_confidence=mapping.overall_confidence,
            build_quantity=max(1, int(options.build_quantity or 1)),
        )
        bom.metadata.update({k: v for k, v in chosen.metadata.items() if v})

        line_no = 1
        for region in regions_to_read:
            region_mapping = mapping
            if region is not chosen:
                region_mapping = mapper.map_columns(
                    region.headers, samples=region.column_samples(),
                    forced=options.forced_mapping or None)
                if region_mapping.missing_required:
                    notes.append(
                        f"Skipped sheet {region.sheet.name!r}: required columns "
                        f"were not found there.")
                    continue
            lines = normalize_rows(
                region.data_rows(), region_mapping,
                sheet_name=region.sheet.name,
                expand_ref_ranges=options.expand_ref_ranges,
                treat_blank_qty_as_one=options.treat_blank_qty_as_one,
                start_line_no=line_no,
            )
            bom.lines.extend(lines)
            bom.sheets_read.append(region.sheet.name)
            line_no += len(lines)

        # Drop rows that turned out to be entirely empty of useful content.
        before = len(bom.lines)
        bom.lines = [
            line for line in bom.lines
            if line.has_part_number or clean(line.description)
            or line.ref_designators or clean(line.internal_pn)
        ]
        dropped = before - len(bom.lines)
        if dropped:
            notes.append(f"Ignored {dropped} row(s) with no part, description "
                         f"or designator.")
        for index, line in enumerate(bom.lines, start=1):
            line.line_no = index

        # 4b) account for every non-empty source row. Silently losing BOM
        # lines is the worst thing an ingester can do, so anything the region
        # detector excluded is reported as a warning the user can act on.
        for region in regions_to_read:
            if region.dropped_rows:
                bom.issues.append(Issue(
                    code="rows_excluded",
                    message=f"{region.dropped_rows} non-empty row(s) below a "
                            f"total/notes row on sheet "
                            f"{region.sheet.name!r} were not read.",
                    severity=Severity.WARNING,
                    suggestion="If those rows are BOM lines, set the header "
                               "row manually or delete the notes row from the "
                               "source file.",
                ))

        # 5) duplicates
        bom.lines, merge_issues = dedupe.merge_duplicates(
            bom.lines, enabled=options.merge_duplicate_mpns)
        bom.issues.extend(merge_issues)
        bom.issues.extend(dedupe.find_refdes_conflicts(bom.lines))
        bom.issues.extend(dedupe.find_near_duplicates(bom.lines))
        bom.issues.extend(dedupe.find_refdes_quantity_mismatch(bom.lines))

        # 6) validation
        bom.issues.extend(validate_mod.validate(
            bom, disabled_rules=options.disabled_rules))

        # 7) learn the layout for next time
        if options.learn_templates and self.template_store is not None \
                and mapping.overall_confidence >= 70 and not mapping.from_template:
            try:
                template_id = mapper.remember(mapping, save_template=True)
                if template_id:
                    mapping.template_id = template_id
                    notes.append("Saved this layout as a reusable template.")
            except Exception as exc:  # pragma: no cover
                LOG.debug("Template learning failed: %s", exc)

        stats = summarise_lines(bom.lines)
        stats["issues"] = validate_mod.issue_counts(
            bom.issues + [i for line in bom.lines for i in line.issues])
        stats["mapping_confidence"] = mapping.overall_confidence

        for note in notes:
            bom.issues.append(Issue(code="ingest_note", message=note,
                                    severity=Severity.INFO))

        LOG.info("Ingested %s: %d lines from %s (mapping %.0f%%)",
                 source_file, len(bom.lines),
                 ", ".join(bom.sheets_read) or "sheet", mapping.overall_confidence)

        return IngestResult(bom=bom, mapping=mapping, regions=regions,
                            chosen=chosen, sheets=list(sheets), stats=stats,
                            notes=notes)


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #

def ingest(path: str | Path, template_store: Any | None = None,
           **option_overrides: Any) -> IngestResult:
    """One-liner used by the CLI and tests."""
    options = IngestOptions()
    for key, value in option_overrides.items():
        if hasattr(options, key):
            setattr(options, key, value)
    return Ingestor(template_store).ingest_file(path, options)


def required_fields() -> tuple[str, ...]:
    return REQUIRED_FIELDS
