"""
Local SQLite persistence: response cache, part cache, column-mapping templates,
saved projects and an audit trail.

Design notes
------------
* One file, ``bomiq.sqlite3``, in the user data directory. Nothing leaves the
  machine.
* WAL journal plus a short busy timeout so the UI thread and worker threads can
  read and write concurrently.
* Every connection is thread-local -- SQLite objects are not shareable across
  threads, and the desktop app runs the analysis on a worker pool.
* Caches are TTL based and can be pruned or cleared from the UI; a corrupted
  database is detected on open and rotated aside rather than crashing the app.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..util import log

LOG = log.get("db")

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_http_cache_expiry ON http_cache(expires_at);

CREATE TABLE IF NOT EXISTS part_cache (
    provider   TEXT NOT NULL,
    mpn_key    TEXT NOT NULL,
    mfr_key    TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (provider, mpn_key, mfr_key)
);
CREATE INDEX IF NOT EXISTS idx_part_cache_expiry ON part_cache(expires_at);

CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    mapping     TEXT NOT NULL,
    options     TEXT NOT NULL DEFAULT '{}',
    use_count   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_fp ON templates(fingerprint);

CREATE TABLE IF NOT EXISTS header_learning (
    header    TEXT NOT NULL,
    field     TEXT NOT NULL,
    hits      INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (header, field)
);

CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS part_notes (
    mpn_key    TEXT NOT NULL,
    field      TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mpn_key, field)
);
"""


class Database:
    """Thread-safe façade over the local SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_schema()

    # -- connection management -------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=15.0, isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @property
    def conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "conn", None)
        if connection is None:
            connection = self._connect()
            self._local.conn = connection
        return connection

    def _init_schema(self) -> None:
        try:
            with self._write_lock:
                self.conn.executescript(_SCHEMA)
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            LOG.error("Local database looks corrupt (%s); rotating it aside.", exc)
            self._rotate_corrupt()
            with self._write_lock:
                self.conn.executescript(_SCHEMA)

    def _rotate_corrupt(self) -> None:
        try:
            if hasattr(self._local, "conn"):
                self._local.conn.close()
                del self._local.conn
        except Exception:  # pragma: no cover
            pass
        backup = self.path.with_suffix(f".corrupt-{int(time.time())}")
        try:
            self.path.rename(backup)
        except OSError:  # pragma: no cover
            try:
                self.path.unlink()
            except OSError:
                pass

    def close(self) -> None:
        connection = getattr(self._local, "conn", None)
        if connection is not None:
            connection.close()
            del self._local.conn

    # -- generic helpers -------------------------------------------------- #

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    # -- maintenance ------------------------------------------------------ #

    def prune(self) -> dict[str, int]:
        now = time.time()
        removed = {}
        with self._write_lock:
            cursor = self.conn.execute(
                "DELETE FROM http_cache WHERE expires_at < ?", (now,))
            removed["http_cache"] = cursor.rowcount or 0
            cursor = self.conn.execute(
                "DELETE FROM part_cache WHERE expires_at < ?", (now,))
            removed["part_cache"] = cursor.rowcount or 0
            cursor = self.conn.execute(
                "DELETE FROM audit WHERE ts < ?", (now - 90 * 86400,))
            removed["audit"] = cursor.rowcount or 0
        return removed

    def clear_caches(self) -> None:
        with self._write_lock:
            self.conn.execute("DELETE FROM http_cache")
            self.conn.execute("DELETE FROM part_cache")
        try:
            self.conn.execute("VACUUM")
        except sqlite3.OperationalError:  # pragma: no cover
            pass

    def stats(self) -> dict[str, Any]:
        def count(table: str) -> int:
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            return int(row["n"]) if row else 0

        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "size_bytes": size,
            "http_cache": count("http_cache"),
            "part_cache": count("part_cache"),
            "templates": count("templates"),
            "projects": count("projects"),
            "learned_headers": count("header_learning"),
        }

    def audit(self, action: str, detail: str = "") -> None:
        try:
            self.execute("INSERT INTO audit(ts, action, detail) VALUES(?,?,?)",
                         (time.time(), action, detail[:2000]))
        except sqlite3.DatabaseError:  # pragma: no cover
            LOG.debug("Audit write failed for %s", action)

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.query(
            "SELECT ts, action, detail FROM audit ORDER BY ts DESC LIMIT ?",
            (limit,))]


# --------------------------------------------------------------------------- #
# Caches
# --------------------------------------------------------------------------- #

class ResponseCache:
    """HTTP response cache with the ``get``/``set`` shape ``HttpClient`` wants."""

    def __init__(self, db: Database, enabled: bool = True) -> None:
        self.db = db
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        row = self.db.query_one(
            "SELECT payload FROM http_cache WHERE key=? AND expires_at > ?",
            (key, time.time()))
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:  # pragma: no cover
            return None

    def set(self, key: str, value: dict[str, Any], ttl: int = 86400) -> None:
        if not self.enabled or ttl <= 0:
            return
        now = time.time()
        try:
            self.db.execute(
                "INSERT INTO http_cache(key, payload, created_at, expires_at) "
                "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "payload=excluded.payload, created_at=excluded.created_at, "
                "expires_at=excluded.expires_at",
                (key, json.dumps(value), now, now + ttl),
            )
        except sqlite3.DatabaseError as exc:  # pragma: no cover
            LOG.debug("Cache write failed: %s", exc)


class PartCache:
    """Cache of normalised :class:`PartData` payloads, keyed per provider."""

    def __init__(self, db: Database, ttl: int = 6 * 3600, enabled: bool = True) -> None:
        self.db = db
        self.ttl = ttl
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def get(self, provider: str, mpn_key: str, mfr_key: str = ""
            ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        row = self.db.query_one(
            "SELECT payload FROM part_cache WHERE provider=? AND mpn_key=? "
            "AND mfr_key=? AND expires_at > ?",
            (provider, mpn_key, mfr_key, time.time()))
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:  # pragma: no cover
            return None

    def set(self, provider: str, mpn_key: str, payload: dict[str, Any],
            mfr_key: str = "", ttl: int | None = None) -> None:
        if not self.enabled:
            return
        now = time.time()
        try:
            self.db.execute(
                "INSERT INTO part_cache(provider, mpn_key, mfr_key, payload, "
                "created_at, expires_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(provider, mpn_key, mfr_key) DO UPDATE SET "
                "payload=excluded.payload, created_at=excluded.created_at, "
                "expires_at=excluded.expires_at",
                (provider, mpn_key, mfr_key, json.dumps(payload), now,
                 now + (ttl if ttl is not None else self.ttl)),
            )
        except sqlite3.DatabaseError as exc:  # pragma: no cover
            LOG.debug("Part cache write failed: %s", exc)


# --------------------------------------------------------------------------- #
# Template store
# --------------------------------------------------------------------------- #

class TemplateStore:
    """Remembers column mappings so a recurring BOM format maps itself.

    Two mechanisms:

    * **Fingerprint templates** -- the sorted set of normalised headers is
      hashed; an exact or near hit replays the whole mapping.
    * **Header learning** -- individual ``header -> field`` confirmations are
      counted, so even a brand-new layout benefits from past corrections.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # fingerprints ------------------------------------------------------- #

    def save(self, name: str, fingerprint: str, mapping: dict[str, str],
             options: dict[str, Any] | None = None) -> str:
        now = time.time()
        row = self.db.query_one(
            "SELECT id, use_count FROM templates WHERE fingerprint=?",
            (fingerprint,))
        if row is not None:
            self.db.execute(
                "UPDATE templates SET name=?, mapping=?, options=?, "
                "use_count=use_count+1, updated_at=? WHERE id=?",
                (name, json.dumps(mapping), json.dumps(options or {}), now,
                 row["id"]),
            )
            return str(row["id"])
        template_id = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO templates(id, name, fingerprint, mapping, options, "
            "use_count, created_at, updated_at) VALUES(?,?,?,?,?,1,?,?)",
            (template_id, name, fingerprint, json.dumps(mapping),
             json.dumps(options or {}), now, now),
        )
        return template_id

    def find(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM templates WHERE fingerprint=?", (fingerprint,))
        return self._row_to_template(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        return [self._row_to_template(row) for row in self.db.query(
            "SELECT * FROM templates ORDER BY use_count DESC, updated_at DESC")]

    def delete(self, template_id: str) -> None:
        self.db.execute("DELETE FROM templates WHERE id=?", (template_id,))

    @staticmethod
    def _row_to_template(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "fingerprint": row["fingerprint"],
            "mapping": json.loads(row["mapping"]),
            "options": json.loads(row["options"] or "{}"),
            "use_count": row["use_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # header learning ---------------------------------------------------- #

    def learn_header(self, header_key: str, field: str, weight: int = 1) -> None:
        if not header_key or not field:
            return
        self.db.execute(
            "INSERT INTO header_learning(header, field, hits, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(header, field) DO UPDATE SET "
            "hits=hits+excluded.hits, updated_at=excluded.updated_at",
            (header_key, field, weight, time.time()),
        )

    def learned_field(self, header_key: str) -> tuple[str | None, int]:
        row = self.db.query_one(
            "SELECT field, hits FROM header_learning WHERE header=? "
            "ORDER BY hits DESC LIMIT 1", (header_key,))
        if row is None:
            return None, 0
        return str(row["field"]), int(row["hits"])

    def learned_map(self) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        for row in self.db.query(
                "SELECT header, field, hits FROM header_learning "
                "ORDER BY hits DESC"):
            out.setdefault(str(row["header"]), (str(row["field"]), int(row["hits"])))
        return out


# --------------------------------------------------------------------------- #
# Project store
# --------------------------------------------------------------------------- #

class ProjectStore:
    """Saves analyses so a user can reopen and compare them later."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, name: str, payload: dict[str, Any], source: str = "",
             summary: dict[str, Any] | None = None,
             project_id: str | None = None) -> str:
        now = time.time()
        pid = project_id or uuid.uuid4().hex[:12]
        blob = json.dumps(payload, separators=(",", ":"))
        self.db.execute(
            "INSERT INTO projects(id, name, source, payload, summary, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "source=excluded.source, payload=excluded.payload, "
            "summary=excluded.summary, updated_at=excluded.updated_at",
            (pid, name, source, blob, json.dumps(summary or {}), now, now),
        )
        self.db.audit("project.save", f"{pid} {name}")
        return pid

    def load(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT payload FROM projects WHERE id=?",
                                (project_id,))
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:  # pragma: no cover
            return None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, name, source, summary, created_at, updated_at "
            "FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.get("summary") or "{}")
            except json.JSONDecodeError:
                item["summary"] = {}
            out.append(item)
        return out

    def delete(self, project_id: str) -> None:
        self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self.db.audit("project.delete", project_id)


class PartNotes:
    """User overrides and notes that survive across analyses."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def set(self, mpn_key: str, field: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO part_notes(mpn_key, field, value, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(mpn_key, field) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (mpn_key, field, value, time.time()),
        )

    def get(self, mpn_key: str) -> dict[str, str]:
        return {
            str(row["field"]): str(row["value"])
            for row in self.db.query(
                "SELECT field, value FROM part_notes WHERE mpn_key=?", (mpn_key,))
        }

    def all(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for row in self.db.query("SELECT mpn_key, field, value FROM part_notes"):
            out.setdefault(str(row["mpn_key"]), {})[str(row["field"])] = \
                str(row["value"])
        return out
