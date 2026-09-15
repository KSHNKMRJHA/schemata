"""Engine lifecycle management + async bridge for FastAPI.

Provides a singleton Engine instance and helpers for running blocking
BOM-IQ methods in a thread pool so the FastAPI event loop stays free.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.engine.bomiq.config import Config
from app.engine.bomiq.engine import Engine
from app.engine.bomiq.core.errors import BomIQError, MappingError, ReadError
from app.engine.bomiq.version import APP_TITLE, __version__

logger = logging.getLogger("schemata_bom.engine")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bom-engine")
_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Get or lazily create the singleton Engine (thread-safe)."""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        logger.info("Creating BOM engine (Schemata v%s)", __version__)
        _engine = Engine()
        return _engine


def reset_engine() -> None:
    """Shut down and drop the singleton (for testing / restart)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.close()
            except Exception:
                pass
            _engine = None


async def run_in_engine(method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking Engine method in a thread pool and await the result."""
    engine = get_engine()
    method = getattr(engine, method_name)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: method(*args, **kwargs))


# ---------------------------------------------------------------------------
# Job tracking (in-memory, replaces BOM-IQ's JobManager for FastAPI)
# ---------------------------------------------------------------------------

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _new_job_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def create_job(label: str, meta: dict[str, Any] | None = None) -> str:
    job_id = _new_job_id()
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "label": label,
            "state": "queued",
            "progress": 0.0,
            "stage": "",
            "message": "Queued",
            "meta": meta or {},
            "events": [],
            "result": None,
            "error": None,
            "error_kind": None,
            "created_at": time.time(),
        }
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def update_job(job_id: str, **updates: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(updates)


def set_job_error(job_id: str, error: str, kind: str = "Error") -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["state"] = "error"
            _jobs[job_id]["error"] = error
            _jobs[job_id]["error_kind"] = kind


def set_job_result(job_id: str, result: dict[str, Any]) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["state"] = "done"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["progress"] = 100.0


def cancel_job(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job["state"] in ("queued", "running"):
            job["state"] = "cancelled"
            job["message"] = "Cancelled by user"
            return True
    return False


def job_progress_callback(job_id: str):
    """Return a ProgressFn-compatible callback for Engine.analyse_bom()."""
    def _on_progress(progress) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None or job["state"] == "cancelled":
                raise BomIQError("Job cancelled")
            job["state"] = "running"
            job["progress"] = progress.percent
            job["stage"] = progress.stage
            job["message"] = progress.message
            job["events"].append({
                "stage": progress.stage,
                "message": progress.message,
                "percent": progress.percent,
                "elapsed_ms": progress.elapsed_ms,
            })
    return _on_progress


def run_analysis_job(
    job_id: str,
    ingest_result=None,
    source_bytes: bytes | None = None,
    label: str = "upload",
    provider_ids: list[str] | None = None,
) -> None:
    """Blocking analysis worker — runs in a background thread."""
    engine = get_engine()
    try:
        update_job(job_id, state="running", message="Analysing…")
        progress = job_progress_callback(job_id)
        if ingest_result is not None:
            analysis = engine.analyse_bom(
                ingest_result.bom,
                progress=progress,
                provider_ids=provider_ids,
            )
        elif source_bytes is not None:
            analysis = engine.analyse_bytes(
                source_bytes,
                name=label,
                progress=progress,
                provider_ids=provider_ids,
            )
        else:
            raise BomIQError("No data to analyse")
        analysis_id = engine.save_project(analysis, name=label)
        set_job_result(job_id, {
            "analysis_id": analysis_id,
            "analysis": analysis.to_dict(),
        })
    except BomIQError as exc:
        set_job_error(job_id, str(exc), type(exc).__name__)
    except Exception as exc:
        logger.exception("Analysis job failed: %s", exc)
        set_job_error(job_id, str(exc), type(exc).__name__)
