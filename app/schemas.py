"""Pydantic schemas for API boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    mpn: str = Field(min_length=1, max_length=255)
    manufacturer: str | None = None
    package: str | None = None
    constraints: dict[str, Any] | None = None


class PriceBreak(BaseModel):
    qty: int
    price: float
    currency: str = "USD"


class SnapshotOut(BaseModel):
    distributor: str
    available: bool
    available_qty: int | None
    price_breaks: list[PriceBreak] = []
    moq: int | None
    lead_time: str
    currency: str
    region: str
    url: str
    checked_at: datetime


class EvidenceOut(BaseModel):
    source_name: str
    source_type: str
    source_url: str
    retrieved_at: datetime
    region: str
    currency: str
    normalized_value: dict[str, Any] | None
    confidence: float
    status: str


class LifecycleEventOut(BaseModel):
    event_type: str
    old_status: str
    new_status: str
    event_date: datetime | None
    source_name: str
    notes: str


class AlternativeIn(BaseModel):
    mpn: str
    manufacturer: str = ""
    category: str = ""
    description: str = ""
    package: str = ""
    lifecycle_status: str = "UNKNOWN"
    operating_voltage: str = ""
    current: str = ""
    frequency: str = ""
    temperature: str = ""
    available: bool = False
    available_qty: int | None = None
    min_price: float | None = None


class AlternativeScore(BaseModel):
    mpn: str
    manufacturer: str
    category: str
    package: str
    lifecycle_status: str
    kind: str  # DROP_IN | FUNCTIONAL | PARAMETRIC
    score: float
    reasons: list[str]


class ComponentOut(BaseModel):
    mpn_normalized: str
    mpn_raw: str
    manufacturer: str
    manufacturer_mpn: str
    family: str
    category: str
    description: str
    package: str
    lifecycle_status: str
    datasheet_url: str
    product_url: str
    rohs: str
    reach: str
    operating_voltage: str
    current: str
    frequency: str
    temperature: str
    updated_at: datetime
    last_verified: datetime | None
    evidences: list[EvidenceOut] = []
    snapshots: list[SnapshotOut] = []
    lifecycle_events: list[LifecycleEventOut] = []


class RiskOut(BaseModel):
    score: int
    level: str  # LOW | MEDIUM | HIGH | CRITICAL
    breakdown: dict[str, float]
    notes: list[str]


class PartReportOut(BaseModel):
    component: ComponentOut
    risk: RiskOut
    alternatives: list[AlternativeScore]
    confidence: float


class BomLine(BaseModel):
    seq: int
    reference: str
    mpn: str
    manufacturer: str = ""
    qty: int = 1
    target_price: float | None = None
    component: ComponentOut | None = None
    risk: RiskOut | None = None
    alternatives: list[AlternativeScore] = []
    cost_estimate: float | None = None
    errors: list[str] = []


class BomReportOut(BaseModel):
    filename: str
    total_lines: int
    ok_lines: int
    error_lines: int
    lifecycle_distribution: dict[str, int]
    risk_distribution: dict[str, int]
    critical_parts: list[BomLine]
    total_cost_estimate: float | None
    currency: str
    lines: list[BomLine]
