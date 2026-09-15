"""Populate the database from the built-in demo catalog with backdated
price/stock snapshots so history charts and trends are meaningful on first run.

The tools.seed_demo CLI delegates here; installed builds call
`ensure_demo_seed()` (app.ensure_demo) so the packaged app starts pre-loaded.
"""

from __future__ import annotations

import asyncio
import math
from datetime import timedelta

from sqlalchemy import select

from app.config import currency, region
from app.db import init_db, session_scope
from app.models import AvailabilitySnapshot, Component
from app.service import persist
from app.sources.demo_catalog import CATALOG
from app.sources.local import digikey_stub, farnell_stub, manufacturer_stub, mouser_stub
from app.util import utcnow
from app.validate import merge_results


async def _collect_stubs(mpn: str):
    adapters = [manufacturer_stub, mouser_stub, digikey_stub, farnell_stub]
    results = await asyncio.gather(*(a.search(mpn, "", region(), currency()) for a in adapters))
    return results


def _add_history(mpn: str, snapshot_count: int = 4, backfill_days: int = 56) -> None:
    from app.sources.demo_catalog import get

    part = get(mpn)
    if part is None or not part.offers:
        return
    now = utcnow()
    with session_scope() as s:
        comp = s.execute(select(Component).where(Component.mpn_normalized == mpn)).scalar_one()
        for i in range(snapshot_count):
            age_days = (snapshot_count - 1 - i) * (backfill_days / snapshot_count)
            checked_at = now - timedelta(days=age_days, hours=i * 7)
            wave = math.sin(i * 1.7 + sum(ord(c) for c in mpn)) * 0.02
            for label, demo in part.offers.items():
                if not demo["breaks"]:
                    continue
                qty = max(0, int(demo["stock"] * (1 + 0.12 * math.sin(i * 0.9 + len(mpn)))))
                breaks = [
                    {"qty": b[0], "price": round(b[1] * (1 + wave), 4), "currency": currency()} for b in demo["breaks"]
                ]
                s.add(
                    AvailabilitySnapshot(
                        component_id=comp.id,
                        distributor=label,
                        available_qty=qty,
                        available=qty > 0,
                        price_breaks=breaks,
                        moq=breaks[0]["qty"] if breaks else None,
                        lead_time=demo["lead"],
                        currency=currency(),
                        region=region(),
                        url=demo["url"],
                        checked_at=checked_at,
                    )
                )


async def seed_all() -> None:
    init_db()
    mpns = sorted(CATALOG)
    for mpn in mpns:
        results = await _collect_stubs(mpn)
        merged = merge_results(results)
        if not merged.facts.manufacturer:
            continue
        persist(results, mpn.upper())
        _add_history(mpn.upper())
    print(f"Seeded {len(mpns)} parts from demo catalog.")


if __name__ == "__main__":
    asyncio.run(seed_all())
