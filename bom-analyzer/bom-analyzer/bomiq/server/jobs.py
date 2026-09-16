"""
Background job manager.

Analysis runs on a worker thread so the HTTP server (and therefore the UI)
stays responsive, progress can be polled, and a long run can be cancelled.
Jobs are kept in memory with their results; finished jobs are reaped after an
idle period so a long-lived desktop session does not grow without bound.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.errors import BomIQError, CancelledError
from ..engine import CancelToken, Progress
from ..util import log

LOG = log.get("server.jobs")

JOB_TTL_SECONDS = 3600
MAX_FINISHED_JOBS = 40


@dataclass
class Job:
    id: str
    kind: str
    label: str = ""
    state: str = "queued"          # queued | running | done | error | cancelled
    progress: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str = ""
    error_kind: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel: CancelToken = field(default_factory=CancelToken)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return int((end - start) * 1000)

    def to_dict(self, include_events: int = 12) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "progress": dict(self.progress),
            "error": self.error,
            "error_kind": self.error_kind,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at,
            "finished_at": self.finished_at or None,
            "events": self.events[-include_events:],
            "meta": dict(self.meta),
            "has_result": self.result is not None,
        }


class JobManager:
    """Runs callables on worker threads and tracks their progress."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # -- submission ------------------------------------------------------- #

    def submit(self, kind: str, label: str,
               work: Callable[[Job], Any],
               meta: dict[str, Any] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label,
                  meta=meta or {})
        with self._lock:
            self._jobs[job.id] = job
            self._reap_locked()
        thread = threading.Thread(target=self._run, args=(job, work),
                                  name=f"bomiq-job-{job.id}", daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, work: Callable[[Job], Any]) -> None:
        job.state = "running"
        job.started_at = time.time()
        try:
            job.result = work(job)
            job.state = "cancelled" if job.cancel.cancelled else "done"
            job.progress = {"stage": "finalise", "message": "Complete.",
                            "percent": 100.0}
        except CancelledError:
            job.state = "cancelled"
            job.error = "Cancelled."
            job.error_kind = "CancelledError"
        except BomIQError as exc:
            job.state = "error"
            job.error = str(exc)
            job.error_kind = type(exc).__name__
            LOG.warning("Job %s failed: %s", job.id, exc)
        except Exception as exc:  # pragma: no cover - defensive
            job.state = "error"
            job.error = f"Unexpected error: {exc}"
            job.error_kind = type(exc).__name__
            LOG.error("Job %s crashed:\n%s", job.id, traceback.format_exc())
        finally:
            job.finished_at = time.time()

    # -- progress --------------------------------------------------------- #

    @staticmethod
    def progress_callback(job: Job) -> Callable[[Progress], None]:
        def report(event: Progress) -> None:
            payload = event.to_dict()
            job.progress = payload
            if not job.events or job.events[-1].get("message") != \
                    payload.get("message"):
                job.events.append(payload)
                if len(job.events) > 400:
                    del job.events[:200]
        return report

    # -- access ----------------------------------------------------------- #

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state in ("done", "error", "cancelled"):
            return False
        job.cancel.cancel()
        return True

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: -j.created_at)
        return [job.to_dict(include_events=0) for job in jobs[:30]]

    def _reap_locked(self) -> None:
        now = time.time()
        finished = [
            job for job in self._jobs.values()
            if job.state in ("done", "error", "cancelled")
        ]
        stale = [job for job in finished
                 if now - (job.finished_at or now) > JOB_TTL_SECONDS]
        for job in stale:
            self._jobs.pop(job.id, None)
        remaining = sorted(
            (job for job in self._jobs.values()
             if job.state in ("done", "error", "cancelled")),
            key=lambda j: j.finished_at or 0,
        )
        while len(remaining) > MAX_FINISHED_JOBS:
            oldest = remaining.pop(0)
            self._jobs.pop(oldest.id, None)
