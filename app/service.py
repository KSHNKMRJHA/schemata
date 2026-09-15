"""Service layer: search pipeline, persistence, risk/alternatives assembly."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import schemas
from app.alternatives import rank_alternatives
from app.config import config_section
from app.db import session_scope
from app.engine.parts_bridge import (
    catalogue_parts,
    lookup_part,
    part_to_candidate,
    source_status,
)
from app.history import build_history
from app.models import (
    AvailabilitySnapshot,
    Component,
    Evidence,
    LifecycleEvent,
)
from app.normalizer import normalize
from app.risk import assess
from app.util import utcnow
from app.validate import merge_results

_COMP_LOAD = [
    selectinload(Component.snapshots),
    selectinload(Component.evidences),
    selectinload(Component.lifecycle_events),
]


def _load_component(session, mpn_normalized: str, must: bool = False) -> Component | None:
    stmt = select(Component).where(Component.mpn_normalized == mpn_normalized).options(*_COMP_LOAD)
    if must:
        return session.execute(stmt).scalar_one()
    return session.execute(stmt).scalar_one_or_none()


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _cache_ttl_hours() -> float:
    return float(config_section("cache").get("component_ttl_hours", 24))


def _snapshot_ttl_minutes() -> float:
    return float(config_section("cache").get("snapshot_ttl_minutes", 15))


def _needs_fetch(component: Component | None, force: bool) -> bool:
    if component is None:
        return True
    if force:
        return True
    if component.last_verified is None:
        return True
    facts_fresh = (utcnow() - _naive(component.last_verified)) <= timedelta(hours=_cache_ttl_hours())
    if not facts_fresh:
        return True
    snaps = component.snapshots
    if snaps:
        newest = max(s.checked_at for s in snaps)
        stock_fresh = (utcnow() - _naive(newest)) <= timedelta(minutes=_snapshot_ttl_minutes())
        if not stock_fresh:
            return True
    return False


async def fetch_and_store(mpn: str, manufacturer: str | None = None) -> Component:
    norm = normalize(mpn, manufacturer)
    results = await lookup_part(norm.mpn_normalized, norm.manufacturer_hint)
    return persist(results, norm.mpn_normalized)


def persist(results: list, mpn_normalized: str) -> Component:
    merged = merge_results(results)
    facts = merged.facts
    now = utcnow()
    with session_scope() as s:
        comp = s.execute(select(Component).where(Component.mpn_normalized == mpn_normalized)).scalar_one_or_none()
        if comp is None:
            comp = Component(mpn_normalized=mpn_normalized, created_at=now)
            s.add(comp)
            s.flush()

        for field_name, value in (
            ("mpn_raw", mpn_normalized),
            ("manufacturer", facts.manufacturer),
            ("manufacturer_mpn", facts.manufacturer_mpn),
            ("family", facts.family),
            ("category", facts.category),
            ("description", facts.description),
            ("package", facts.package),
            ("lifecycle_status", facts.lifecycle_status),
            ("datasheet_url", facts.datasheet_url),
            ("product_url", facts.product_url),
            ("rohs", facts.rohs),
            ("reach", facts.reach),
            ("operating_voltage", facts.operating_voltage),
            ("current", facts.current),
            ("frequency", facts.frequency),
            ("temperature", facts.temperature),
        ):
            if value:
                setattr(comp, field_name, value)
        comp.updated_at = now
        comp.last_verified = now

        s.query(Evidence).filter(Evidence.component_id == comp.id).delete()
        for ev in merged.evidence:
            s.add(
                Evidence(
                    component_id=comp.id,
                    source_name=ev.source_name,
                    source_type=ev.source_type,
                    source_url=ev.source_url,
                    retrieved_at=_naive(ev.retrieved_at),
                    region=ev.region,
                    currency=ev.currency,
                    normalized_value=ev.normalized_value,
                    confidence=ev.confidence,
                    status=ev.status,
                    doc_reference=ev.doc_reference,
                )
            )

        for offer in merged.offers:
            breaks = [{"qty": b.qty, "price": b.price, "currency": b.currency} for b in offer.price_breaks]
            if not (offer.available_qty or breaks):
                continue
            s.add(
                AvailabilitySnapshot(
                    component_id=comp.id,
                    distributor=offer.distributor,
                    available_qty=offer.available_qty,
                    available=offer.available,
                    price_breaks=breaks or None,
                    moq=offer.moq,
                    lead_time=offer.lead_time,
                    currency=offer.currency,
                    region=offer.region,
                    url=offer.url,
                    checked_at=now,
                )
            )

        s.flush()
        comp = s.execute(select(Component).where(Component.id == comp.id)).scalar_one()

        # Lifecycle notices ride on the manufacturer source result (not facts).
        for res in results:
            for notice in res.lifecycle_notices:
                exists = (
                    s.query(LifecycleEvent)
                    .filter(
                        LifecycleEvent.component_id == comp.id,
                        LifecycleEvent.event_type == notice.event_type,
                    )
                    .first()
                )
                if exists:
                    continue
                s.add(
                    LifecycleEvent(
                        component_id=comp.id,
                        event_type=notice.event_type,
                        old_status=comp.lifecycle_status,
                        new_status=comp.lifecycle_status,
                        event_date=notice.event_date,
                        source_name=notice.source_name,
                        source_url=notice.source_url,
                        notes=notice.notes,
                    )
                )
        return comp


def component_report_dict(comp: Component) -> dict:
    return {
        "mpn": comp.mpn_normalized,
        "mpn_normalized": comp.mpn_normalized,
        "manufacturer": comp.manufacturer,
        "category": comp.category,
        "description": comp.description,
        "package": comp.package,
        "lifecycle_status": comp.lifecycle_status,
        "operating_voltage": comp.operating_voltage,
        "current": comp.current,
        "frequency": comp.frequency,
        "temperature": comp.temperature,
        "available": any(s.available for s in comp.snapshots),
        "available_qty": sum(s.available_qty or 0 for s in comp.snapshots),
        "min_price": _min_price_of(comp),
    }


def _min_price_of(comp: Component):
    prices = []
    for s in comp.snapshots:
        for pb in s.price_breaks or []:
            if pb.get("price") is not None:
                prices.append(pb["price"])
    return min(prices) if prices else None


def _candidates(target: dict) -> list[dict]:
    pool: list[dict] = []
    with session_scope() as s:
        for comp in s.execute(select(Component)).scalars():
            if comp.mpn_normalized == target["mpn_normalized"]:
                continue
            pool.append(component_report_dict(comp))
    for part in catalogue_parts():
        if part.mpn.upper() == target["mpn_normalized"].upper():
            continue
        pool.append(part_to_candidate(part))
    return pool


def to_component_out(comp: Component) -> schemas.ComponentOut:
    snaps = sorted(comp.snapshots, key=lambda x: x.checked_at, reverse=True)
    evs = comp.evidences
    events = sorted(comp.lifecycle_events, key=lambda x: x.event_date or x.id)
    return schemas.ComponentOut(
        mpn_normalized=comp.mpn_normalized,
        mpn_raw=comp.mpn_raw,
        manufacturer=comp.manufacturer,
        manufacturer_mpn=comp.manufacturer_mpn,
        family=comp.family,
        category=comp.category,
        description=comp.description,
        package=comp.package,
        lifecycle_status=comp.lifecycle_status,
        datasheet_url=comp.datasheet_url,
        product_url=comp.product_url,
        rohs=comp.rohs,
        reach=comp.reach,
        operating_voltage=comp.operating_voltage,
        current=comp.current,
        frequency=comp.frequency,
        temperature=comp.temperature,
        updated_at=comp.updated_at,
        last_verified=comp.last_verified,
        evidences=[
            schemas.EvidenceOut(
                source_name=e.source_name,
                source_type=e.source_type,
                source_url=e.source_url,
                retrieved_at=e.retrieved_at,
                region=e.region,
                currency=e.currency,
                normalized_value=e.normalized_value,
                confidence=e.confidence,
                status=e.status,
            )
            for e in evs
        ],
        snapshots=[
            schemas.SnapshotOut(
                distributor=s.distributor,
                available=s.available,
                available_qty=s.available_qty,
                price_breaks=[schemas.PriceBreak(**pb) for pb in (s.price_breaks or [])],
                moq=s.moq,
                lead_time=s.lead_time,
                currency=s.currency,
                region=s.region,
                url=s.url,
                checked_at=s.checked_at,
            )
            for s in snaps
        ],
        lifecycle_events=[
            schemas.LifecycleEventOut(
                event_type=e.event_type,
                old_status=e.old_status,
                new_status=e.new_status,
                event_date=e.event_date,
                source_name=e.source_name,
                notes=e.notes,
            )
            for e in events
        ],
    )


def _to_alt_schema(items) -> list[schemas.AlternativeScore]:
    return [schemas.AlternativeScore(**vars(i)) for i in items]


async def get_part_report(mpn: str, manufacturer: str | None = None, force: bool = False) -> schemas.PartReportOut:
    norm = normalize(mpn, manufacturer)
    with session_scope() as s:
        comp = _load_component(s, norm.mpn_normalized)

    if _needs_fetch(comp, force):
        comp = await fetch_and_store(norm.mpn_normalized, norm.manufacturer_hint or manufacturer)

    with session_scope() as s:
        comp = _load_component(s, norm.mpn_normalized, must=True)
        history = build_history(comp.snapshots)
        risk = assess(comp, history, source_status()["live"])
        target = component_report_dict(comp)
        alts = rank_alternatives(target, _candidates(target))

    overall_confidence = 0.0
    if comp.evidences:
        overall_confidence = round(sum(e.confidence for e in comp.evidences) / len(comp.evidences), 2)

    return schemas.PartReportOut(
        component=to_component_out(comp),
        risk=schemas.RiskOut(score=risk.score, level=risk.level, breakdown=risk.breakdown, notes=risk.notes),
        alternatives=_to_alt_schema(alts),
        confidence=overall_confidence,
    )


def recent_parts(limit: int = 50) -> list[Component]:
    with session_scope() as s:
        rows = (
            s.execute(select(Component).options(*_COMP_LOAD).order_by(Component.updated_at.desc()).limit(limit))
            .scalars()
            .all()
        )
        return list(rows)
