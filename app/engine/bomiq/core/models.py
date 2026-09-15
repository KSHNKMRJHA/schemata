"""
Domain model for the Schemata BOM engine.

Plain dataclasses, no third-party validation library, with explicit
``to_dict``/``from_dict`` so every object round-trips through JSON for the REST
API, the cache and the project file format.

Money is carried as ``Decimal`` in memory and serialised as a string so no
precision is lost in the project file.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from typing import Any

from ..util.money import to_decimal
from ..util.text import clean, collapse_refdes


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #

class Lifecycle(str, enum.Enum):
    """Normalised part lifecycle state.

    Distributors each use their own vocabulary; :mod:`bomiq.analysis.lifecycle`
    maps all of them onto these six values.
    """

    NEW = "New"
    ACTIVE = "Active"
    NRND = "NRND"
    EOL = "EOL"
    OBSOLETE = "Obsolete"
    UNKNOWN = "Unknown"

    @property
    def is_risky(self) -> bool:
        return self in (Lifecycle.NRND, Lifecycle.EOL, Lifecycle.OBSOLETE)

    @property
    def rank(self) -> int:
        """Lower is healthier."""
        return {
            Lifecycle.ACTIVE: 0, Lifecycle.NEW: 1, Lifecycle.UNKNOWN: 2,
            Lifecycle.NRND: 3, Lifecycle.EOL: 4, Lifecycle.OBSOLETE: 5,
        }[self]


class ComplianceState(str, enum.Enum):
    COMPLIANT = "Compliant"
    NON_COMPLIANT = "Non-compliant"
    EXEMPT = "Exempt"
    UNKNOWN = "Unknown"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}[self]


class MatchKind(str, enum.Enum):
    """How a BOM line was tied to catalogue data."""

    EXACT = "exact"                 # MPN identical after normalisation
    EXACT_WITH_MFR = "exact+mfr"    # MPN and manufacturer both agree
    ROOT = "root"                   # matched after stripping packaging suffix
    FUZZY = "fuzzy"                 # similarity above threshold
    DESCRIPTION = "description"     # matched via parametric/keyword search
    DISTRIBUTOR_SKU = "sku"         # matched on a distributor part number
    NONE = "none"

    @property
    def base_confidence(self) -> int:
        return {
            MatchKind.EXACT_WITH_MFR: 100, MatchKind.EXACT: 92,
            MatchKind.ROOT: 82, MatchKind.DISTRIBUTOR_SKU: 88,
            MatchKind.FUZZY: 60, MatchKind.DESCRIPTION: 45, MatchKind.NONE: 0,
        }[self]


class RiskLevel(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"

    @classmethod
    def from_score(cls, score: float) -> "RiskLevel":
        """Map a 0-100 risk score (higher = worse) onto a band."""
        if score >= 75:
            return cls.CRITICAL
        if score >= 50:
            return cls.HIGH
        if score >= 25:
            return cls.MEDIUM
        return cls.LOW


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #

def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_encode(v) for v in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class Serialisable:
    """Mixin giving dataclasses symmetric dict conversion."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            out[f.name] = _encode(getattr(self, f.name))
        return out

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #

@dataclass
class Issue(Serialisable):
    """A validation finding or analysis note attached to a line or the BOM."""

    code: str
    message: str
    severity: Severity = Severity.WARNING
    field: str | None = None
    line_no: int | None = None
    suggestion: str | None = None
    auto_fixable: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            code=data.get("code", "unknown"),
            message=data.get("message", ""),
            severity=Severity(data.get("severity", "warning")),
            field=data.get("field"),
            line_no=data.get("line_no"),
            suggestion=data.get("suggestion"),
            auto_fixable=bool(data.get("auto_fixable", False)),
        )


# --------------------------------------------------------------------------- #
# BOM input
# --------------------------------------------------------------------------- #

@dataclass
class BomLine(Serialisable):
    """One normalised line of the customer's BOM.

    ``raw`` keeps every original cell so nothing the user gave us is ever lost,
    and exports can echo unmapped columns back out.
    """

    line_no: int
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    quantity: float = 1.0
    ref_designators: list[str] = field(default_factory=list)
    internal_pn: str = ""
    value: str = ""
    package: str = ""
    tolerance: str = ""
    voltage: str = ""
    power: str = ""
    alt_mpns: list[str] = field(default_factory=list)
    distributor: str = ""
    distributor_pn: str = ""
    unit_price_in: Decimal | None = None
    currency_in: str = ""
    notes: str = ""
    dnp: bool = False
    level: str = ""                       # multi-level BOM indent, e.g. "1.2"
    parent: str = ""
    source_row: int | None = None         # 1-based row in the source file
    source_sheet: str = ""
    raw: dict[str, str] = field(default_factory=dict)
    merged_from: list[int] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    # -- derived ---------------------------------------------------------- #

    @property
    def ref_text(self) -> str:
        return collapse_refdes(self.ref_designators)

    @property
    def has_part_number(self) -> bool:
        return bool(clean(self.mpn)) or bool(clean(self.distributor_pn))

    @property
    def effective_quantity(self) -> float:
        """Quantity used for costing (0 for do-not-populate lines)."""
        return 0.0 if self.dnp else max(0.0, self.quantity)

    @property
    def search_terms(self) -> list[str]:
        """Candidate identifiers to try against providers, best first."""
        terms: list[str] = []
        for candidate in [self.mpn, self.distributor_pn, *self.alt_mpns]:
            text = clean(candidate)
            if text and text not in terms:
                terms.append(text)
        return terms

    def add_issue(self, code: str, message: str,
                  severity: Severity = Severity.WARNING, **kwargs: Any) -> None:
        self.issues.append(Issue(code=code, message=message, severity=severity,
                                 line_no=self.line_no, **kwargs))

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["ref_text"] = self.ref_text
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BomLine":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["issues"] = [
            Issue.from_dict(i) if isinstance(i, dict) else i
            for i in data.get("issues", [])
        ]
        kwargs["unit_price_in"] = to_decimal(data.get("unit_price_in"))
        kwargs["quantity"] = float(data.get("quantity") or 0.0)
        return cls(**kwargs)


@dataclass
class Bom(Serialisable):
    """A parsed BOM: header metadata plus normalised lines."""

    name: str = ""
    source_file: str = ""
    source_format: str = ""
    lines: list[BomLine] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    column_map: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    template_name: str = ""
    template_confidence: float = 0.0
    issues: list[Issue] = field(default_factory=list)
    header_row: int | None = None
    sheets_read: list[str] = field(default_factory=list)
    build_quantity: int = 1

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def placement_count(self) -> int:
        return int(sum(line.effective_quantity for line in self.lines))

    @property
    def unique_mpns(self) -> int:
        return len({clean(line.mpn).upper() for line in self.lines if line.mpn})

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["line_count"] = self.line_count
        data["placement_count"] = self.placement_count
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Bom":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["lines"] = [BomLine.from_dict(line) for line in data.get("lines", [])]
        kwargs["issues"] = [Issue.from_dict(i) for i in data.get("issues", [])]
        return cls(**kwargs)


# --------------------------------------------------------------------------- #
# Provider output
# --------------------------------------------------------------------------- #

@dataclass
class PriceBreak(Serialisable):
    quantity: int
    unit_price: Decimal
    currency: str = "USD"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PriceBreak":
        return cls(
            quantity=int(data.get("quantity") or 1),
            unit_price=to_decimal(data.get("unit_price")) or Decimal("0"),
            currency=data.get("currency") or "USD",
        )


@dataclass
class Offer(Serialisable):
    """A purchasable offer for a part from one distributor."""

    provider: str                       # provider id, e.g. "digikey"
    distributor: str                    # human name, e.g. "DigiKey"
    sku: str = ""
    packaging: str = ""
    stock: int | None = None
    on_order: int | None = None
    factory_stock: int | None = None
    moq: int | None = None
    spq: int | None = None
    order_multiple: int | None = None
    lead_time_days: int | None = None
    price_breaks: list[PriceBreak] = field(default_factory=list)
    currency: str = "USD"
    url: str = ""
    region: str = ""
    authorized: bool = True
    last_updated: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def in_stock(self) -> bool:
        return bool(self.stock and self.stock > 0)

    @property
    def real_price_breaks(self) -> list["PriceBreak"]:
        """Only the breaks that carry an actual quote.

        A unit price of exactly 0 (or, from a malformed feed, a negative one)
        is a "call for pricing" placeholder, not a free part. Letting one
        through made an unquotable line cost 0.00 and report as fully
        covered, so every costing path reads the ladder through here rather
        than off ``price_breaks`` directly.
        """
        return [b for b in self.price_breaks
                if b.unit_price is not None and b.unit_price > 0]

    @property
    def min_unit_price(self) -> Decimal | None:
        prices = [b.unit_price for b in self.real_price_breaks]
        return min(prices) if prices else None

    @property
    def unit_price_at_one(self) -> Decimal | None:
        ladder = sorted(self.real_price_breaks, key=lambda b: b.quantity)
        return ladder[0].unit_price if ladder else None

    @property
    def break_tuples(self) -> list[tuple[int, Decimal]]:
        return [(b.quantity, b.unit_price) for b in self.real_price_breaks]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Offer":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["price_breaks"] = [
            PriceBreak.from_dict(b) if isinstance(b, dict) else b
            for b in data.get("price_breaks", [])
        ]
        return cls(**kwargs)


@dataclass
class Compliance(Serialisable):
    rohs: ComplianceState = ComplianceState.UNKNOWN
    rohs_note: str = ""
    reach: ComplianceState = ComplianceState.UNKNOWN
    reach_note: str = ""
    svhc: list[str] = field(default_factory=list)
    halogen_free: ComplianceState = ComplianceState.UNKNOWN
    conflict_minerals: str = ""
    country_of_origin: str = ""
    hts_code: str = ""
    eccn: str = ""
    export_controlled: bool | None = None
    itar: bool | None = None
    msl: str = ""
    aec_q: str = ""
    lead_free_process: str = ""
    sources: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Compliance":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        for key in ("rohs", "reach", "halogen_free"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = ComplianceState(kwargs[key])
        return cls(**kwargs)


@dataclass
class PartData(Serialisable):
    """Consolidated catalogue record for one manufacturer part."""

    mpn: str
    manufacturer: str = ""
    description: str = ""
    category: str = ""
    series: str = ""
    package: str = ""
    mount: str = ""
    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    lifecycle_note: str = ""
    lifecycle_sources: dict[str, str] = field(default_factory=dict)
    datasheet_url: str = ""
    image_url: str = ""
    product_url: str = ""
    specs: dict[str, str] = field(default_factory=dict)
    offers: list[Offer] = field(default_factory=list)
    compliance: Compliance = field(default_factory=Compliance)
    alternate_mpns: list[str] = field(default_factory=list)
    similar_mpns: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    median_price_1k: Decimal | None = None
    #: currency of ``median_price_1k``. Providers return this figure in
    #: whatever currency they were queried in, so it must travel with the
    #: number -- converting a JPY figure as if it were USD is a 150x error.
    median_price_1k_currency: str = "USD"
    estimated_factory_lead_days: int | None = None
    total_avail: int | None = None
    fetched_at: str = ""

    @property
    def authorized_offers(self) -> list[Offer]:
        return [o for o in self.offers if o.authorized]

    @property
    def in_stock_offers(self) -> list[Offer]:
        return [o for o in self.offers if o.in_stock]

    @property
    def total_stock(self) -> int:
        return sum(o.stock or 0 for o in self.offers)

    @property
    def distributor_count(self) -> int:
        return len({o.distributor for o in self.offers if o.in_stock}) or \
            len({o.distributor for o in self.offers})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PartData":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["offers"] = [
            Offer.from_dict(o) if isinstance(o, dict) else o
            for o in data.get("offers", [])
        ]
        compliance = data.get("compliance")
        kwargs["compliance"] = (
            Compliance.from_dict(compliance) if isinstance(compliance, dict)
            else (compliance or Compliance())
        )
        if data.get("lifecycle"):
            kwargs["lifecycle"] = Lifecycle(data["lifecycle"])
        kwargs["median_price_1k"] = to_decimal(data.get("median_price_1k"))
        return cls(**kwargs)


# --------------------------------------------------------------------------- #
# Analysis output
# --------------------------------------------------------------------------- #

@dataclass
class Match(Serialisable):
    kind: MatchKind = MatchKind.NONE
    confidence: int = 0
    matched_mpn: str = ""
    matched_manufacturer: str = ""
    provider: str = ""
    reasons: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    needs_review: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Match":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        if data.get("kind"):
            kwargs["kind"] = MatchKind(data["kind"])
        return cls(**kwargs)


@dataclass
class RiskFactor(Serialisable):
    code: str
    label: str
    score: float           # 0..100 contribution before weighting
    weight: float          # relative weight
    detail: str = ""


@dataclass
class Risk(Serialisable):
    score: float = 0.0                  # 0..100, higher = riskier
    level: RiskLevel = RiskLevel.LOW
    factors: list[RiskFactor] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Risk":
        return cls(
            score=float(data.get("score") or 0),
            level=RiskLevel(data.get("level", "Low")),
            factors=[RiskFactor(**f) for f in data.get("factors", [])],
            flags=list(data.get("flags", [])),
        )


@dataclass
class SourcingOption(Serialisable):
    """One way to buy the required quantity of a line."""

    provider: str
    distributor: str
    sku: str = ""
    order_qty: int = 0
    unit_price: Decimal | None = None
    break_qty: int | None = None
    extended: Decimal | None = None
    currency: str = "USD"
    native_unit_price: Decimal | None = None
    native_currency: str = ""
    stock: int | None = None
    lead_time_days: int | None = None
    moq: int | None = None
    spq: int | None = None
    covers_demand: bool = True
    overbuy_qty: int = 0
    overbuy_cost: Decimal | None = None
    url: str = ""
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourcingOption":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        for key in ("unit_price", "extended", "native_unit_price", "overbuy_cost"):
            kwargs[key] = to_decimal(data.get(key))
        return cls(**kwargs)


@dataclass
class Cost(Serialisable):
    """Costing for one line at the analysed build quantity."""

    required_qty: int = 0
    best: SourcingOption | None = None
    options: list[SourcingOption] = field(default_factory=list)
    unit_price: Decimal | None = None
    extended: Decimal | None = None
    currency: str = "USD"
    price_curve: list[dict[str, Any]] = field(default_factory=list)
    savings_vs_worst: Decimal | None = None
    price_spread_pct: float | None = None
    indicative_fx: bool = False
    #: True when the price is an aggregated estimate rather than a real,
    #: purchasable offer -- the UI and the report both label these.
    estimated: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cost":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["best"] = (
            SourcingOption.from_dict(data["best"]) if data.get("best") else None
        )
        kwargs["options"] = [SourcingOption.from_dict(o)
                             for o in data.get("options", [])]
        for key in ("unit_price", "extended", "savings_vs_worst"):
            kwargs[key] = to_decimal(data.get(key))
        return cls(**kwargs)


@dataclass
class Alternate(Serialisable):
    """A suggested substitute part."""

    mpn: str
    manufacturer: str = ""
    description: str = ""
    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    score: int = 0                       # 0..100 suitability
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    stock: int | None = None
    unit_price: Decimal | None = None
    currency: str = "USD"
    price_delta_pct: float | None = None
    source: str = ""                     # how it was found
    url: str = ""
    pin_compatible: bool | None = None
    spec_deltas: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alternate":
        known = cls.field_names()
        kwargs = {k: v for k, v in data.items() if k in known}
        if data.get("lifecycle"):
            kwargs["lifecycle"] = Lifecycle(data["lifecycle"])
        kwargs["unit_price"] = to_decimal(data.get("unit_price"))
        return cls(**kwargs)


@dataclass
class Completion(Serialisable):
    """A field the engine filled in or corrected, with provenance."""

    field: str
    old_value: str
    new_value: str
    source: str
    confidence: int = 0
    applied: bool = False


@dataclass
class LineResult(Serialisable):
    """Everything the engine concluded about one BOM line."""

    line: BomLine
    match: Match = field(default_factory=Match)
    part: PartData | None = None
    risk: Risk = field(default_factory=Risk)
    cost: Cost = field(default_factory=Cost)
    compliance: Compliance = field(default_factory=Compliance)
    alternates: list[Alternate] = field(default_factory=list)
    completions: list[Completion] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    provider_errors: dict[str, str] = field(default_factory=dict)

    @property
    def all_issues(self) -> list[Issue]:
        return sorted(self.line.issues + self.issues,
                      key=lambda i: -i.severity.rank)

    @property
    def max_severity(self) -> Severity:
        issues = self.all_issues
        return max((i.severity for i in issues), key=lambda s: s.rank,
                   default=Severity.INFO)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["max_severity"] = self.max_severity.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LineResult":
        return cls(
            line=BomLine.from_dict(data["line"]),
            match=Match.from_dict(data.get("match", {})),
            part=PartData.from_dict(data["part"]) if data.get("part") else None,
            risk=Risk.from_dict(data.get("risk", {})),
            cost=Cost.from_dict(data.get("cost", {})),
            compliance=Compliance.from_dict(data.get("compliance", {})),
            alternates=[Alternate.from_dict(a) for a in data.get("alternates", [])],
            completions=[Completion(**c) for c in data.get("completions", [])],
            issues=[Issue.from_dict(i) for i in data.get("issues", [])],
            provider_errors=dict(data.get("provider_errors", {})),
        )


@dataclass
class HealthScore(Serialisable):
    """Whole-BOM health rollup."""

    score: float = 0.0                  # 0..100, higher = healthier
    grade: str = "?"
    level: RiskLevel = RiskLevel.LOW
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    headline: str = ""
    drivers: list[str] = field(default_factory=list)


@dataclass
class AnalysisSummary(Serialisable):
    total_lines: int = 0
    dnp_lines: int = 0
    costed_lines: int = 0
    matched_lines: int = 0
    unmatched_lines: int = 0
    review_lines: int = 0
    placements: int = 0
    unique_parts: int = 0
    build_quantity: int = 1
    currency: str = "USD"
    total_cost: Decimal | None = None
    cost_per_unit: Decimal | None = None
    cost_coverage_pct: float = 0.0
    potential_savings: Decimal | None = None
    lifecycle_counts: dict[str, int] = field(default_factory=dict)
    risk_counts: dict[str, int] = field(default_factory=dict)
    severity_counts: dict[str, int] = field(default_factory=dict)
    compliance_counts: dict[str, int] = field(default_factory=dict)
    single_source_lines: int = 0
    out_of_stock_lines: int = 0
    long_lead_lines: int = 0
    max_lead_time_days: int | None = None
    provider_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    indicative_fx: bool = False
    #: the settings that shaped this analysis, echoed so a saved project or an
    #: exported report always states the rules it was judged against
    rules: dict[str, Any] = field(default_factory=dict)


@dataclass
class BomAnalysis(Serialisable):
    """Top-level analysis result. This is what the UI and exporters consume."""

    bom: Bom
    results: list[LineResult] = field(default_factory=list)
    summary: AnalysisSummary = field(default_factory=AnalysisSummary)
    health: HealthScore = field(default_factory=HealthScore)
    issues: list[Issue] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    engine_version: str = ""
    providers_used: list[str] = field(default_factory=list)
    offline: bool = False
    warnings: list[str] = field(default_factory=list)

    def result_for_line(self, line_no: int) -> LineResult | None:
        for result in self.results:
            if result.line.line_no == line_no:
                return result
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BomAnalysis":
        analysis = cls(bom=Bom.from_dict(data["bom"]))
        analysis.results = [LineResult.from_dict(r) for r in data.get("results", [])]
        summary = data.get("summary") or {}
        known = AnalysisSummary.field_names()
        analysis.summary = AnalysisSummary(
            **{k: v for k, v in summary.items() if k in known})
        analysis.summary.total_cost = to_decimal(summary.get("total_cost"))
        analysis.summary.cost_per_unit = to_decimal(summary.get("cost_per_unit"))
        analysis.summary.potential_savings = to_decimal(
            summary.get("potential_savings"))
        health = data.get("health") or {}
        analysis.health = HealthScore(
            score=float(health.get("score") or 0),
            grade=health.get("grade", "?"),
            level=RiskLevel(health.get("level", "Low")),
            components=dict(health.get("components", {})),
            weights=dict(health.get("weights", {})),
            headline=health.get("headline", ""),
            drivers=list(health.get("drivers", [])),
        )
        analysis.issues = [Issue.from_dict(i) for i in data.get("issues", [])]
        for key in ("started_at", "finished_at", "engine_version"):
            setattr(analysis, key, data.get(key, ""))
        analysis.duration_ms = int(data.get("duration_ms") or 0)
        analysis.providers_used = list(data.get("providers_used", []))
        analysis.offline = bool(data.get("offline"))
        analysis.warnings = list(data.get("warnings", []))
        return analysis


__all__ = [
    "Lifecycle", "ComplianceState", "Severity", "MatchKind", "RiskLevel",
    "Issue", "BomLine", "Bom", "PriceBreak", "Offer", "Compliance", "PartData",
    "Match", "RiskFactor", "Risk", "SourcingOption", "Cost", "Alternate",
    "Completion", "LineResult", "HealthScore", "AnalysisSummary", "BomAnalysis",
]
