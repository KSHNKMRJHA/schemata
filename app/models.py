"""ORM models: Component + Evidence + AvailabilitySnapshot + LifecycleEvent."""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.util import utcnow


class LifecycleStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    NRND = "NRND"
    EOL_ANNOUNCED = "EOL_ANNOUNCED"
    LAST_TIME_BUY = "LAST_TIME_BUY"
    OBSOLETE = "OBSOLETE"
    UNKNOWN = "UNKNOWN"


class SourceType(enum.StrEnum):
    MANUFACTURER = "manufacturer"
    DISTRIBUTOR = "distributor"
    MOCK = "mock"


class EvidenceStatus(enum.StrEnum):
    VERIFIED = "verified"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class LifecycleEventType(enum.StrEnum):
    EOL_ANNOUNCED = "EOL_ANNOUNCED"
    LAST_TIME_BUY = "LAST_TIME_BUY"
    LAST_SHIPMENT = "LAST_SHIPMENT"
    PCN = "PCN"
    PDN = "PDN"
    SUCCESSOR = "SUCCESSOR"


class Component(Base):
    __tablename__ = "components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mpn_normalized: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    mpn_raw: Mapped[str] = mapped_column(String(255), default="")
    manufacturer: Mapped[str] = mapped_column(String(255), default="")
    manufacturer_mpn: Mapped[str] = mapped_column(String(255), default="")
    family: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    package: Mapped[str] = mapped_column(String(255), default="")
    lifecycle_status: Mapped[str] = mapped_column(
        SAEnum(LifecycleStatus, values_callable=lambda e: [x.value for x in e]), default=LifecycleStatus.UNKNOWN.value
    )
    datasheet_url: Mapped[str] = mapped_column(String(1024), default="")
    product_url: Mapped[str] = mapped_column(String(1024), default="")
    rohs: Mapped[str] = mapped_column(String(64), default="Unknown")
    reach: Mapped[str] = mapped_column(String(64), default="Unknown")
    operating_voltage: Mapped[str] = mapped_column(String(255), default="")
    current: Mapped[str] = mapped_column(String(255), default="")
    frequency: Mapped[str] = mapped_column(String(255), default="")
    temperature: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_verified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    evidences: Mapped[list[Evidence]] = relationship(back_populates="component", cascade="all, delete-orphan")
    snapshots: Mapped[list[AvailabilitySnapshot]] = relationship(
        back_populates="component", cascade="all, delete-orphan"
    )
    lifecycle_events: Mapped[list[LifecycleEvent]] = relationship(
        back_populates="component", cascade="all, delete-orphan"
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(SAEnum(SourceType, values_callable=lambda e: [x.value for x in e]))
    source_url: Mapped[str] = mapped_column(String(1024), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    region: Mapped[str] = mapped_column(String(16), default="")
    currency: Mapped[str] = mapped_column(String(16), default="")
    raw_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    normalized_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    doc_reference: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(
        SAEnum(EvidenceStatus, values_callable=lambda e: [x.value for x in e]),
        default=EvidenceStatus.UNKNOWN.value,
    )

    component: Mapped[Component] = relationship(back_populates="evidences")


class AvailabilitySnapshot(Base):
    __tablename__ = "availability_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id"), index=True)
    distributor: Mapped[str] = mapped_column(String(255))
    available_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available: Mapped[bool] = mapped_column(default=False)
    price_breaks: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{qty, price, currency}]
    moq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lead_time: Mapped[str] = mapped_column(String(255), default="")
    currency: Mapped[str] = mapped_column(String(16), default="")
    region: Mapped[str] = mapped_column(String(16), default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    component: Mapped[Component] = relationship(back_populates="snapshots")


class LifecycleEvent(Base):
    __tablename__ = "lifecycle_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id"), index=True)
    event_type: Mapped[str] = mapped_column(SAEnum(LifecycleEventType, values_callable=lambda e: [x.value for x in e]))
    old_status: Mapped[str] = mapped_column(String(64), default="")
    new_status: Mapped[str] = mapped_column(String(64), default="")
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_name: Mapped[str] = mapped_column(String(255), default="")
    source_url: Mapped[str] = mapped_column(String(1024), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    component: Mapped[Component] = relationship(back_populates="lifecycle_events")
