"""Small shared helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC now — matches what the SQLite/DateTime columns store."""
    return datetime.now(UTC).replace(tzinfo=None)
