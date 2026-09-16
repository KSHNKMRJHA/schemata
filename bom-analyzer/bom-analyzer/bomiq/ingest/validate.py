"""
BOM validation rule engine.

Each rule is a small function that inspects one line (or the whole BOM) and
returns issues. Rules are registered in a table so the set can be extended,
disabled per-project, and reported on. Everything is deterministic and runs
before any network call, so a user with no API keys still gets a full
data-quality audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..core.models import Bom, BomLine, Issue, Severity
from ..util.text import clean, normalize_mpn, refdes_prefix, tokens
from ..util.units import (
    normalize_package, parse_quantity, parse_tolerance, parse_value,
)

LineRule = Callable[[BomLine], Iterable[Issue]]
BomRule = Callable[[Bom], Iterable[Issue]]


@dataclass(frozen=True)
class Rule:
    code: str
    title: str
    severity: Severity
    scope: str                  # "line" | "bom"
    description: str
    enabled_by_default: bool = True


RULES: dict[str, Rule] = {}
_LINE_RULES: list[tuple[Rule, LineRule]] = []
_BOM_RULES: list[tuple[Rule, BomRule]] = []


def line_rule(code: str, title: str, severity: Severity, description: str,
              enabled: bool = True):
    def decorator(func: LineRule) -> LineRule:
        rule = Rule(code, title, severity, "line", description, enabled)
        RULES[code] = rule
        _LINE_RULES.append((rule, func))
        return func
    return decorator


def bom_rule(code: str, title: str, severity: Severity, description: str,
             enabled: bool = True):
    def decorator(func: BomRule) -> BomRule:
        rule = Rule(code, title, severity, "bom", description, enabled)
        RULES[code] = rule
        _BOM_RULES.append((rule, func))
        return func
    return decorator


def _issue(code: str, message: str, severity: Severity, line: BomLine | None = None,
           field: str | None = None, suggestion: str | None = None,
           auto_fixable: bool = False) -> Issue:
    return Issue(code=code, message=message, severity=severity, field=field,
                 line_no=line.line_no if line else None, suggestion=suggestion,
                 auto_fixable=auto_fixable)


# --------------------------------------------------------------------------- #
# Line rules
# --------------------------------------------------------------------------- #

_SUSPICIOUS_MPN_RE = re.compile(r"^[\d\s.,\-]+$")
_EXCEL_ERROR_RE = re.compile(r"^#(N/A|VALUE!|REF!|NAME\?|DIV/0!|NULL!|NUM!)$",
                             re.IGNORECASE)
_SCIENTIFIC_RE = re.compile(r"^\d(\.\d+)?E[+\-]?\d+$", re.IGNORECASE)


@line_rule("mpn_suspicious", "Part number looks wrong", Severity.WARNING,
           "Catches values that are numeric-only, Excel errors, or that Excel "
           "has mangled into scientific notation.")
def _rule_mpn_suspicious(line: BomLine) -> Iterable[Issue]:
    mpn = clean(line.mpn)
    if not mpn:
        return
    if _EXCEL_ERROR_RE.match(mpn):
        yield _issue("mpn_excel_error",
                     f"The part number cell contains the Excel error {mpn}.",
                     Severity.ERROR, line, "mpn",
                     "Fix the formula in the source file and re-export.")
        return
    if _SCIENTIFIC_RE.match(mpn):
        yield _issue("mpn_scientific",
                     f"{mpn!r} looks like a part number Excel converted to "
                     f"scientific notation.", Severity.ERROR, line, "mpn",
                     "Format the column as Text in the source file, or export "
                     "to CSV with quoting.")
        return
    if _SUSPICIOUS_MPN_RE.match(mpn) and len(mpn) < 5:
        yield _issue("mpn_numeric",
                     f"{mpn!r} is unusually short and entirely numeric for a "
                     f"part number.", Severity.WARNING, line, "mpn")
    if len(mpn) < 3:
        yield _issue("mpn_too_short",
                     f"{mpn!r} is too short to identify a part.",
                     Severity.WARNING, line, "mpn")
    if len(mpn) > 60:
        yield _issue("mpn_too_long",
                     "The part number is over 60 characters — it may "
                     "contain a description as well.",
                     Severity.WARNING, line, "mpn")
    if "  " in line.mpn or line.mpn != line.mpn.strip():
        yield _issue("mpn_whitespace",
                     "The part number contains stray whitespace.",
                     Severity.INFO, line, "mpn", "Trimmed automatically.",
                     auto_fixable=True)
    if re.search(r"[À-ɏ一-鿿]", mpn):
        yield _issue("mpn_non_ascii",
                     "The part number contains non-ASCII characters, which "
                     "distributor APIs usually reject.",
                     Severity.WARNING, line, "mpn")


@line_rule("qty_invalid", "Quantity is not usable", Severity.ERROR,
           "Zero, negative or fractional quantities on populated lines.")
def _rule_quantity(line: BomLine) -> Iterable[Issue]:
    if line.dnp:
        return
    if line.quantity < 0:
        yield _issue("qty_negative",
                     f"Quantity {line.quantity:g} is negative.",
                     Severity.ERROR, line, "quantity")
    elif line.quantity == 0:
        yield _issue("qty_zero",
                     "Quantity is zero but the line is not marked "
                     "do-not-populate.", Severity.WARNING, line, "quantity",
                     "Mark it DNP, or set a real quantity.")
    elif not float(line.quantity).is_integer():
        prefixes = {refdes_prefix(r) for r in line.ref_designators}
        # Fractional quantities are legitimate for bulk materials.
        bulk = bool(prefixes & {"", "SOL", "ADH", "PST"}) or not line.ref_designators
        yield _issue("qty_fractional",
                     f"Quantity {line.quantity:g} is fractional.",
                     Severity.INFO if bulk else Severity.WARNING,
                     line, "quantity")
    if line.quantity > 100000:
        yield _issue("qty_implausible",
                     f"Quantity {line.quantity:g} is unusually large for one "
                     f"assembly — check the units.",
                     Severity.WARNING, line, "quantity")


@line_rule("manufacturer_missing", "Manufacturer not given", Severity.INFO,
           "Manufacturer is optional but greatly improves match accuracy.")
def _rule_manufacturer(line: BomLine) -> Iterable[Issue]:
    if line.mpn and not line.manufacturer and not line.dnp:
        yield _issue("manufacturer_missing",
                     "No manufacturer given; part numbers are occasionally "
                     "reused across vendors.", Severity.INFO, line,
                     "manufacturer",
                     "BOM-IQ will fill this in from catalogue data.",
                     auto_fixable=True)
    if line.manufacturer and len(clean(line.manufacturer)) < 2:
        yield _issue("manufacturer_short",
                     f"Manufacturer {line.manufacturer!r} is too short to be "
                     f"meaningful.", Severity.WARNING, line, "manufacturer")


@line_rule("refdes_issues", "Reference designator problems", Severity.WARNING,
           "Malformed designators and prefixes that disagree with the part "
           "description.")
def _rule_refdes(line: BomLine) -> Iterable[Issue]:
    if not line.ref_designators:
        return
    malformed = [
        ref for ref in line.ref_designators
        if not re.fullmatch(r"[A-Z]{1,4}\d{1,6}[A-Z]?", ref)
    ]
    if malformed:
        sample = ", ".join(malformed[:5])
        yield _issue("refdes_malformed",
                     f"{len(malformed)} reference designator(s) are not in the "
                     f"usual letter+number form: {sample}.",
                     Severity.INFO, line, "ref_designators")
    duplicates = [
        ref for ref in set(line.ref_designators)
        if line.ref_designators.count(ref) > 1
    ]
    if duplicates:
        yield _issue("refdes_duplicate_in_line",
                     f"Reference designator(s) repeated on this line: "
                     f"{', '.join(sorted(duplicates)[:5])}.",
                     Severity.WARNING, line, "ref_designators")

    prefixes = {refdes_prefix(ref) for ref in line.ref_designators} - {""}
    if len(prefixes) > 1:
        yield _issue("refdes_mixed_prefix",
                     f"This line mixes designator families "
                     f"({', '.join(sorted(prefixes))}) — usually a sign "
                     f"two parts were combined.",
                     Severity.WARNING, line, "ref_designators")

    expected = _expected_prefix(line)
    if expected and prefixes and not (prefixes & expected):
        yield _issue("refdes_prefix_mismatch",
                     f"Designator prefix {'/'.join(sorted(prefixes))} does not "
                     f"match the described component type "
                     f"(expected {'/'.join(sorted(expected))}).",
                     Severity.INFO, line, "ref_designators")


_TYPE_PREFIXES: list[tuple[re.Pattern[str], set[str]]] = [
    (re.compile(r"\b(resistor|res|thermistor|potentiometer|trimmer)\b", re.I),
     {"R", "RN", "RV", "RT", "VR", "P", "RP"}),
    (re.compile(r"\b(capacitor|cap|mlcc|tantalum|electrolytic)\b", re.I),
     {"C", "CP", "CE", "CT"}),
    (re.compile(r"\b(inductor|choke|ferrite|bead|coil)\b", re.I),
     {"L", "FB", "FL"}),
    (re.compile(r"\b(diode|zener|schottky|tvs|rectifier)\b", re.I),
     {"D", "CR", "ZD", "TVS", "Z"}),
    (re.compile(r"\b(led|light emitting)\b", re.I), {"D", "LED", "DS", "L"}),
    (re.compile(r"\b(transistor|mosfet|bjt|igbt|fet)\b", re.I),
     {"Q", "T", "TR", "M"}),
    (re.compile(r"\b(connector|header|receptacle|socket|terminal|jack|plug)\b",
                re.I), {"J", "P", "CN", "CON", "X", "TB", "S"}),
    (re.compile(r"\b(crystal|oscillator|resonator|xtal)\b", re.I),
     {"X", "Y", "XT", "OSC", "Q"}),
    (re.compile(r"\b(switch|button|tactile|encoder)\b", re.I),
     {"SW", "S", "BTN", "PB"}),
    (re.compile(r"\b(fuse|ptc|polyfuse)\b", re.I), {"F", "FU"}),
    (re.compile(r"\b(relay|contactor)\b", re.I), {"K", "RL", "RY"}),
    (re.compile(r"\b(transformer)\b", re.I), {"T", "TR"}),
    (re.compile(r"\b(test ?point|testpoint)\b", re.I), {"TP", "T"}),
    (re.compile(r"\b(antenna)\b", re.I), {"E", "ANT", "AE"}),
    (re.compile(r"\b(battery|holder|coin cell)\b", re.I), {"BT", "BAT", "B"}),
    (re.compile(r"\b(mounting hole|standoff|screw|nut|washer|spacer|bracket)\b",
                re.I), {"MH", "H", "HW", "MP"}),
    (re.compile(r"\b(module|shield|board|pcb|assembly)\b", re.I),
     {"U", "M", "A", "MOD", "PCB"}),
]


def _expected_prefix(line: BomLine) -> set[str]:
    text = f"{line.description} {line.value}"
    for pattern, prefixes in _TYPE_PREFIXES:
        if pattern.search(text):
            return prefixes
    return set()


@line_rule("parametric_mismatch", "Value and description disagree",
           Severity.WARNING,
           "Cross-checks the Value column against numbers in the description.")
def _rule_value_vs_description(line: BomLine) -> Iterable[Issue]:
    if not line.value or not line.description:
        return
    value, kind = parse_value(line.value)
    if value is None:
        return
    if value == 0:
        # A 0 ohm jumper has nothing to cross-check, and dividing by it would
        # raise inside the rule engine.
        return
    for candidate in tokens(line.description)[:24]:
        other, other_kind = parse_value(candidate)
        if other is None or other_kind != kind or other == 0:
            continue
        ratio = max(value, other) / min(value, other)
        if 1.02 < ratio < 1e6 and abs(ratio - 1000) > 1 and \
                abs(ratio - 1e6) > 1:
            yield _issue("value_description_mismatch",
                         f"Value {line.value!r} does not match "
                         f"{candidate!r} in the description.",
                         Severity.WARNING, line, "value")
            return
        return


@line_rule("tolerance_invalid", "Tolerance not understood", Severity.INFO,
           "Checks the tolerance column parses to a percentage.")
def _rule_tolerance(line: BomLine) -> Iterable[Issue]:
    if line.tolerance and parse_tolerance(line.tolerance) is None:
        yield _issue("tolerance_unparsed",
                     f"Tolerance {line.tolerance!r} could not be interpreted.",
                     Severity.INFO, line, "tolerance")


@line_rule("package_unknown", "Footprint not recognised", Severity.INFO,
           "Flags footprints that cannot be normalised, which weakens "
           "alternate-part checking.")
def _rule_package(line: BomLine) -> Iterable[Issue]:
    if not line.package:
        return
    normalised = normalize_package(line.package)
    if not normalised or len(normalised) < 2:
        yield _issue("package_unparsed",
                     f"Footprint {line.package!r} was not recognised; "
                     f"drop-in checks for alternates will be weaker.",
                     Severity.INFO, line, "package")


@line_rule("dnp_with_price", "DNP line carries a price", Severity.INFO,
           "A do-not-populate line with a price is usually a leftover.")
def _rule_dnp_price(line: BomLine) -> Iterable[Issue]:
    if line.dnp and line.unit_price_in:
        yield _issue("dnp_with_price",
                     "This line is marked do-not-populate but still has a "
                     "price; it is excluded from the cost roll-up.",
                     Severity.INFO, line, "dnp")


@line_rule("alt_mpn_sanity", "Alternate part numbers look wrong",
           Severity.INFO, "Validates the alternates column.")
def _rule_alternates(line: BomLine) -> Iterable[Issue]:
    for alt in line.alt_mpns:
        if normalize_mpn(alt) == normalize_mpn(line.mpn):
            yield _issue("alt_equals_primary",
                         f"Alternate {alt!r} is the same as the primary part "
                         f"number.", Severity.INFO, line, "alt_mpns")
        elif len(clean(alt)) < 3:
            yield _issue("alt_too_short",
                         f"Alternate {alt!r} is too short to be a part number.",
                         Severity.INFO, line, "alt_mpns")


# --------------------------------------------------------------------------- #
# BOM-level rules
# --------------------------------------------------------------------------- #

@bom_rule("bom_empty", "No usable lines", Severity.ERROR,
          "The file parsed but produced no BOM lines.")
def _rule_bom_empty(bom: Bom) -> Iterable[Issue]:
    if not bom.lines:
        yield Issue(code="bom_empty",
                    message="No BOM lines were found in this file.",
                    severity=Severity.ERROR,
                    suggestion="Check that the correct sheet was selected and "
                               "that the header row was detected properly.")


@bom_rule("mpn_coverage", "Too few part numbers", Severity.WARNING,
          "Warns when a large share of lines have no part number, which caps "
          "how much the analysis can tell you.")
def _rule_mpn_coverage(bom: Bom) -> Iterable[Issue]:
    populated = [line for line in bom.lines if not line.dnp]
    if not populated:
        return
    without = [line for line in populated if not line.has_part_number]
    share = len(without) / len(populated)
    if share > 0.5:
        yield Issue(code="mpn_coverage_low",
                    message=f"{len(without)} of {len(populated)} populated "
                            f"lines ({share * 100:.0f}%) have no part number.",
                    severity=Severity.ERROR,
                    suggestion="Check the column mapping — the part number "
                               "column may not have been detected.")
    elif share > 0.15:
        yield Issue(code="mpn_coverage_partial",
                    message=f"{len(without)} populated line(s) have no part "
                            f"number and cannot be priced or risk-scored.",
                    severity=Severity.WARNING)


@bom_rule("mapping_confidence", "Column mapping uncertain", Severity.WARNING,
          "Raised when the automatic column mapping was not confident.")
def _rule_mapping_confidence(bom: Bom) -> Iterable[Issue]:
    if bom.template_confidence and bom.template_confidence < 70:
        yield Issue(code="mapping_uncertain",
                    message=f"Column mapping confidence is "
                            f"{bom.template_confidence:.0f}%. Review the "
                            f"mapping before relying on the results.",
                    severity=Severity.WARNING,
                    suggestion="Open the column mapping panel and confirm each "
                               "field.")


@bom_rule("unmapped_columns", "Unused columns", Severity.INFO,
          "Lists columns that were carried through but not interpreted.")
def _rule_unmapped(bom: Bom) -> Iterable[Issue]:
    interesting = [
        name for name in bom.unmapped_columns
        if clean(name) and not re.fullmatch(r"Column \d+", clean(name))
    ]
    if len(interesting) > 0:
        yield Issue(code="unmapped_columns",
                    message=f"{len(interesting)} column(s) were not "
                            f"interpreted: {', '.join(interesting[:8])}"
                            f"{'…' if len(interesting) > 8 else ''}. They "
                            f"are preserved in the export.",
                    severity=Severity.INFO)


@bom_rule("single_line_bom", "Suspiciously small BOM", Severity.WARNING,
          "A one- or two-line BOM usually means the wrong table was picked.")
def _rule_small_bom(bom: Bom) -> Iterable[Issue]:
    if 0 < len(bom.lines) <= 2:
        yield Issue(code="bom_very_small",
                    message=f"Only {len(bom.lines)} line(s) were found. If the "
                            f"file has more, the wrong sheet or header row may "
                            f"have been chosen.",
                    severity=Severity.WARNING)


@bom_rule("mixed_currency", "Input prices use several currencies",
          Severity.INFO, "Detects mixed currencies in the price column.")
def _rule_mixed_currency(bom: Bom) -> Iterable[Issue]:
    currencies = {line.currency_in for line in bom.lines if line.currency_in}
    if len(currencies) > 1:
        yield Issue(code="mixed_currency",
                    message=f"The price column mixes currencies: "
                            f"{', '.join(sorted(currencies))}. Figures are "
                            f"converted for comparison.",
                    severity=Severity.INFO)


@bom_rule("dnp_share", "Large share of DNP lines", Severity.INFO,
          "Informational: many DNP lines may mean a variant BOM.")
def _rule_dnp_share(bom: Bom) -> Iterable[Issue]:
    if not bom.lines:
        return
    dnp = sum(1 for line in bom.lines if line.dnp)
    if dnp and dnp / len(bom.lines) > 0.3:
        yield Issue(code="dnp_share_high",
                    message=f"{dnp} of {len(bom.lines)} lines "
                            f"({dnp / len(bom.lines) * 100:.0f}%) are marked "
                            f"do-not-populate.",
                    severity=Severity.INFO)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def validate(bom: Bom, disabled_rules: Sequence[str] = ()) -> list[Issue]:
    """Run every enabled rule. Line issues are attached to their line; the
    returned list holds the BOM-level findings."""
    disabled = set(disabled_rules)
    for rule, func in _LINE_RULES:
        if rule.code in disabled or not rule.enabled_by_default:
            continue
        for line in bom.lines:
            try:
                for issue in func(line):
                    line.issues.append(issue)
            except Exception as exc:  # pragma: no cover - a rule must not kill
                line.add_issue("rule_error",
                               f"Validation rule {rule.code} failed: {exc}",
                               Severity.INFO)

    findings: list[Issue] = []
    for rule, bom_func in _BOM_RULES:
        if rule.code in disabled or not rule.enabled_by_default:
            continue
        try:
            findings.extend(bom_func(bom))
        except Exception as exc:  # pragma: no cover
            findings.append(Issue(code="rule_error",
                                  message=f"Validation rule {rule.code} "
                                          f"failed: {exc}",
                                  severity=Severity.INFO))
    return findings


def describe_rules() -> list[dict[str, object]]:
    return [
        {
            "code": rule.code,
            "title": rule.title,
            "severity": rule.severity.value,
            "scope": rule.scope,
            "description": rule.description,
            "enabled_by_default": rule.enabled_by_default,
        }
        for rule in sorted(RULES.values(), key=lambda r: (r.scope, r.code))
    ]


def issue_counts(issues: Iterable[Issue]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue.severity.value] = counts.get(issue.severity.value, 0) + 1
    return counts
