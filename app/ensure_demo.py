"""First-run demo seeding for installed builds (idempotent, fast-path skip)."""

from __future__ import annotations

import asyncio


def ensure_demo_seed() -> None:
    """Seed the demo catalog once so a fresh install isn't an empty library."""
    from sqlalchemy import func, select

    from app.config import get_settings
    from app.db import init_db, session_scope
    from app.models import Component

    init_db()
    if not get_settings().demo_data:
        return
    with session_scope() as s:
        if s.execute(select(func.count(Component.id))).scalar() or 0:
            return  # already populated (or user searched live parts)
    from app.seed_demo import seed_all

    asyncio.run(seed_all())
