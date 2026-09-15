"""Source adapter contracts, shared result types, and a per-source rate limiter."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from app.models import LifecycleStatus, SourceType


@dataclass
class PriceBreak:
    qty: int
    price: float
    currency: str


@dataclass
class Offer:
    distributor: str
    available: bool = False
    available_qty: int | None = None
    price_breaks: list[PriceBreak] = field(default_factory=list)
    moq: int | None = None
    lead_time: str = ""
    currency: str = "USD"
    region: str = "US"
    url: str = ""


@dataclass
class LifecycleNotice:
    event_type: str  # LifecycleEventType value
    event_date: date | None = None
    notes: str = ""
    source_name: str = ""
    source_url: str = ""


@dataclass
class PartFacts:
    manufacturer: str = ""
    manufacturer_mpn: str = ""
    description: str = ""
    category: str = ""
    family: str = ""
    package: str = ""
    lifecycle_status: str = LifecycleStatus.UNKNOWN.value
    datasheet_url: str = ""
    product_url: str = ""
    rohs: str = "Unknown"
    reach: str = "Unknown"
    operating_voltage: str = ""
    current: str = ""
    frequency: str = ""
    temperature: str = ""


@dataclass
class SourceResult:
    source_name: str
    source_type: SourceType
    source_url: str
    retrieved_at: datetime
    region: str
    currency: str
    confidence: float
    facts: PartFacts | None = None
    offers: list[Offer] = field(default_factory=list)
    lifecycle_notices: list[LifecycleNotice] = field(default_factory=list)
    doc_reference: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def is_mock(self) -> bool:
        return self.source_type == SourceType.MOCK


class SourceAdapter(ABC):
    """One adapter per external source. Must be safe to run concurrently."""

    name: str
    source_type: SourceType = SourceType.DISTRIBUTOR

    @abstractmethod
    async def search(self, mpn: str, manufacturer: str, region: str, currency: str) -> SourceResult: ...


class TokenBucket:
    """Simple per-source rate limiter (async)."""

    def __init__(self, rate_per_minute: float, max_burst: int | None = None):
        if rate_per_minute <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate_per_minute / 60.0
        self.capacity = max_burst or max(int(rate_per_minute / 60.0 * 5), 2)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._updated = time.monotonic()
            else:
                self._tokens -= 1

    def as_decorator(self, func):
        async def wrapper(*args, **kwargs):
            await self.acquire()
            return await func(*args, **kwargs)

        return wrapper


def upsert_offer(offers: list[Offer], offer: Offer) -> None:
    """Add or replace an offer by distributor name (case-insensitive)."""
    for i, existing in enumerate(offers):
        if existing.distributor.lower() == offer.distributor.lower():
            offers[i] = offer
            return
    offers.append(offer)


def parse_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default
