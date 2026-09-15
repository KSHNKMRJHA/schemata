"""SQLAlchemy engine and session helpers."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.config import get_settings

_engine = None
_SessionLocal = None


class Base(DeclarativeBase):
    pass


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"connect_args": {"check_same_thread": False}}
        if url.startswith("sqlite"):
            # SQLite cannot share pooled connections across threads safely:
            # use a single shared connection for in-memory DBs and a fresh
            # connection per checkout otherwise (avoids "database is locked").
            is_memory = url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url
            kwargs["poolclass"] = StaticPool if is_memory else NullPool
        _engine = create_engine(url, **kwargs)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def init_db() -> None:
    from app import models  # noqa: F401  (register models)

    Base.metadata.create_all(get_engine())


def session_scope():
    """Context-managed session; yields a session and commits on success."""
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        session = get_session_factory()()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope()
