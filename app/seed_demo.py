"""Populate the database from the engine's offline catalogue with backdated
price/stock snapshots so history charts and trends are meaningful on first run.

The tools.seed_demo CLI delegates here; installed builds call
`ensure_demo_seed()` (app.ensure_demo) so the packaged app starts pre-loaded.
"""

from __future__ import annotations

import asyncio
import math
from datetime import timedelta

from sqlalchemy import select

from app.config import currency
from app.db import init_db, session_scope
from app.engine.parts_bridge import catalogue_parts, lookup_part
from app.models import AvailabilitySnapshot, Component
from app.service import persist
from app.util import utcnow
from app.validate import merge_results


def _add_history(mpn: str, snapshot_count: int = 4, backfill_days: int = 56) -> None:
    now = utcnow()
    with session_scope() as s:
        comp = s.execute(select(Component).where(Component.mpn_normalized == mpn)).scalar_one()
        base_snaps = sorted(comp.snapshots, key=lambda x: x.distributor.lower())
        if not base_snaps:
            return
        for i in range(1, snapshot_count):
            age_days = (snapshot_count - 1 - i) * (backfill_days / snapshot_count)
            checked_at = now - timedelta(days=age_days, hours=i * 7)
            wave = math.sin(i * 1.7 + sum(ord(c) for c in mpn)) * 0.02
            for base in base_snaps:
                qty = 0
                if base.available_qty:
                    qty = max(
                        0,
                        int(base.available_qty * (1 + 0.12 * math.sin(i * 0.9 + len(mpn)))),
                    )
                breaks = []
                if base.price_breaks:
                    breaks = [
                        {
                            "qty": b["qty"],
                            "price": round(b["price"] * (1 + wave), 4),
                            "currency": b.get("currency") or currency(),
                        }
                        for b in base.price_breaks
                    ]
                s.add(
                    AvailabilitySnapshot(
                        component_id=comp.id,
                        distributor=base.distributor,
                        available_qty=qty,
                        available=qty > 0,
                        price_breaks=breaks or None,
                        moq=base.moq,
                        lead_time=base.lead_time,
                        currency=base.currency,
                        region=base.region,
                        url=base.url,
                        checked_at=checked_at,
                    )
                )


async def _seed_part(mpn: str) -> str | None:
    results = await lookup_part(mpn, "")
    merged = merge_results(results)
    if not merged.facts.manufacturer:
        return None
    persist(results, mpn.upper())
    _add_history(mpn.upper())
    return mpn


async def seed_all() -> None:
    init_db()
    mpns = sorted(p.mpn for p in catalogue_parts())
    seeded = [mpn for mpn in mpns if await _seed_part(mpn)]  # noqa: C416
    print(f"Seeded {len(seeded)} parts from the offline catalogue.")


if __name__ == "__main__":
    asyncio.run(seed_all())
