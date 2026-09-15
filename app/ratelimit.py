"""In-process fixed-window rate limiter for API endpoints.

Thread-safe and dependency-free so it also works in the frozen binary build.
The per-client budget is configured in config.toml under ``[api]``:

    [api]
    requests_per_minute = 600   # 0 or a negative value disables the limiter
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from app.config import config_section

_WINDOW_SECONDS = 60
_MAX_CLIENTS = 2048

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}


def _bounded(client_key: str, limit: int, now: float) -> bool:
    cutoff = now - _WINDOW_SECONDS
    with _lock:
        queue = _hits.setdefault(client_key, deque())
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
        if len(_hits) > _MAX_CLIENTS:
            for key in [k for k, q in _hits.items() if not q]:
                _hits.pop(key, None)
        return True


def api_rate_limit(request: Request) -> None:
    """FastAPI dependency: reject clients that exceed the configured API budget."""
    limit = int(config_section("api").get("requests_per_minute", 600))
    if limit <= 0:
        return
    client = request.client.host if request.client else "local"
    if not _bounded(client, limit, time.monotonic()):
        raise HTTPException(status_code=429, detail="Rate limit exceeded — try again shortly.")
