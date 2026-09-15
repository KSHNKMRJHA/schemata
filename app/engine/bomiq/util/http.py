"""
A small, dependency-free HTTP client built on ``urllib.request``.

Why not ``requests``/``httpx``: this application ships as a packaged desktop
binary and must work on a bare Python install. The stdlib gives us everything
we need; what it does not give us -- token-bucket rate limiting, jittered
exponential backoff, ``Retry-After`` handling, response caching and a circuit
breaker -- is implemented here once and shared by every provider.

Thread safety: a single :class:`HttpClient` is safe to use from many threads.
Rate limiting and the circuit breaker are per-host and lock-protected.
"""

from __future__ import annotations

import gzip
import json
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import log

LOG = log.get("http")

DEFAULT_TIMEOUT = 25.0
DEFAULT_USER_AGENT = "Schemata-BOM/2.0 (+local analysis tool)"

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}


class HttpError(Exception):
    """Raised for non-retryable or exhausted HTTP failures."""

    def __init__(self, message: str, status: int | None = None,
                 body: str | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = (body or "")[:4000]
        self.url = url

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429


class CircuitOpen(HttpError):
    """Raised when a host has failed so often that we stop calling it."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise HttpError(
                f"Response from {self.url} was not valid JSON: {exc}",
                status=self.status, body=self.text, url=self.url,
            ) from exc


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

class TokenBucket:
    """Classic token bucket. ``rate`` is requests per second."""

    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = max(rate, 0.01)
        self.capacity = float(burst if burst is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                need = (1.0 - self._tokens) / self.rate
            if time.monotonic() + need > deadline:
                return False
            time.sleep(min(need, 0.25))


@dataclass
class CircuitBreaker:
    """Stop hammering a host that is consistently failing."""

    threshold: int = 6
    cooldown: float = 60.0
    failures: int = 0
    opened_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at and time.monotonic() - self.opened_at < self.cooldown:
                return False
            if self.opened_at:
                # Cooldown elapsed: half-open, let one request through.
                self.opened_at = 0.0
                self.failures = self.threshold - 1
            return True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()
                LOG.warning("Circuit opened for %.0fs after %d failures",
                            self.cooldown, self.failures)

    @property
    def is_open(self) -> bool:
        return bool(self.opened_at) and (
            time.monotonic() - self.opened_at < self.cooldown
        )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class HttpClient:
    """Retrying, rate-limited, optionally caching HTTP client.

    Parameters
    ----------
    rate_per_second:
        Default request rate applied per host.
    cache:
        Any object exposing ``get(key)`` / ``set(key, value, ttl)`` returning
        and accepting ``dict``. :class:`bomiq.core.db.ResponseCache` implements
        this. ``None`` disables caching.
    """

    def __init__(self, *, rate_per_second: float = 5.0, retries: int = 3,
                 timeout: float = DEFAULT_TIMEOUT, cache: Any | None = None,
                 user_agent: str = DEFAULT_USER_AGENT,
                 verify_tls: bool = True, ca_file: str | None = None,
                 proxy: str | None = None) -> None:
        self.default_rate = rate_per_second
        self.retries = max(0, retries)
        self.timeout = timeout
        self.cache = cache
        self.user_agent = user_agent
        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {
            "requests": 0, "cache_hits": 0, "retries": 0, "errors": 0,
            "rate_limited": 0,
        }
        self._stats_lock = threading.Lock()

        if verify_tls:
            self._ssl_context = ssl.create_default_context(cafile=ca_file)
        else:  # pragma: no cover - only reachable via explicit opt-out
            self._ssl_context = ssl._create_unverified_context()

        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=self._ssl_context),
        ]
        if proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    # -- internals -------------------------------------------------------- #

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def _bucket(self, host: str, rate: float | None) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = TokenBucket(rate or self.default_rate)
                self._buckets[host] = bucket
            return bucket

    def _breaker(self, host: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(host)
            if breaker is None:
                breaker = CircuitBreaker()
                self._breakers[host] = breaker
            return breaker

    def set_rate(self, host: str, rate_per_second: float) -> None:
        """Set a host's request rate, preserving the tokens already spent.

        Replacing the bucket would hand out a fresh burst every time a
        provider registry is constructed -- which happens once per analysis and
        once per lookup request -- and that is how a provider's published rate
        gets exceeded.
        """
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                self._buckets[host] = TokenBucket(rate_per_second)
                return
            rate = max(rate_per_second, 0.01)
            with bucket._lock:           # noqa: SLF001 - same module family
                bucket.rate = rate
                bucket.capacity = max(1.0, rate)
                bucket._tokens = min(bucket._tokens, bucket.capacity)

    @staticmethod
    def _decode_body(raw: bytes, headers: Mapping[str, str]) -> bytes:
        encoding = (headers.get("content-encoding") or "").lower()
        if not raw:
            return raw
        try:
            if "gzip" in encoding:
                return gzip.decompress(raw)
            if "deflate" in encoding:
                try:
                    return zlib.decompress(raw)
                except zlib.error:
                    return zlib.decompress(raw, -zlib.MAX_WBITS)
        except (OSError, zlib.error):  # pragma: no cover - malformed payload
            return raw
        return raw

    @staticmethod
    def _cache_key(method: str, url: str, body: bytes | None,
                   headers: Mapping[str, str]) -> str:
        import hashlib

        hasher = hashlib.sha256()
        hasher.update(method.upper().encode())
        hasher.update(b"\x00")
        hasher.update(url.encode())
        hasher.update(b"\x00")
        if body:
            hasher.update(body)
        # Only cache-relevant headers participate in the key.
        for name in ("accept", "accept-language", "x-digikey-locale-currency",
                     "x-digikey-locale-site", "x-digikey-customer-id"):
            value = headers.get(name)
            if value:
                hasher.update(f"{name}={value}".encode())
        return hasher.hexdigest()

    # -- public API ------------------------------------------------------- #

    def request(self, method: str, url: str, *,
                params: Mapping[str, Any] | None = None,
                json_body: Any | None = None,
                data: bytes | Mapping[str, Any] | None = None,
                headers: Mapping[str, str] | None = None,
                timeout: float | None = None,
                rate_per_second: float | None = None,
                cache_ttl: int | None = None,
                retries: int | None = None,
                allow_status: tuple[int, ...] = ()) -> Response:
        """Perform a request with retries, rate limiting and caching."""
        method = method.upper()
        if params:
            clean_params = {
                key: value for key, value in params.items() if value is not None
            }
            if clean_params:
                separator = "&" if urllib.parse.urlparse(url).query else "?"
                url = url + separator + urllib.parse.urlencode(
                    clean_params, doseq=True)

        request_headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
            request_headers.setdefault("Accept", "application/json")
        elif isinstance(data, (dict,)):
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif isinstance(data, bytes):
            body = data
        if headers:
            request_headers.update({str(k): str(v) for k, v in headers.items()})

        lower_headers = {k.lower(): v for k, v in request_headers.items()}
        host = urllib.parse.urlparse(url).netloc

        cache_key = None
        if self.cache is not None and cache_ttl and cache_ttl > 0:
            cache_key = self._cache_key(method, url, body, lower_headers)
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._bump("cache_hits")
                return Response(
                    status=int(cached.get("status", 200)),
                    headers=cached.get("headers", {}),
                    body=(cached.get("body") or "").encode("utf-8"),
                    url=url, from_cache=True,
                )

        breaker = self._breaker(host)
        if not breaker.allow():
            raise CircuitOpen(
                f"Skipping {host}: too many consecutive failures, retrying later.",
                url=url,
            )

        attempts = self.retries if retries is None else max(0, retries)
        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            if not self._bucket(host, rate_per_second).acquire(timeout=90.0):
                raise HttpError(f"Rate limit wait timed out for {host}", url=url)
            started = time.monotonic()
            try:
                request = urllib.request.Request(
                    url, data=body, headers=request_headers, method=method)
                self._bump("requests")
                with self._opener.open(
                        request, timeout=timeout or self.timeout) as handle:
                    raw = handle.read()
                    response_headers = {
                        k.lower(): v for k, v in handle.headers.items()
                    }
                    payload = self._decode_body(raw, response_headers)
                    response = Response(
                        status=handle.status, headers=response_headers,
                        body=payload, url=handle.url or url,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                breaker.record_success()
                if cache_key is not None and 200 <= response.status < 300:
                    self.cache.set(cache_key, {
                        "status": response.status,
                        "headers": response.headers,
                        "body": response.body.decode("utf-8", errors="replace"),
                    }, ttl=cache_ttl)
                return response

            except urllib.error.HTTPError as exc:
                raw = b""
                try:
                    raw = exc.read()
                except Exception:  # pragma: no cover
                    pass
                response_headers = {
                    k.lower(): v for k, v in (exc.headers or {}).items()
                }
                payload = self._decode_body(raw, response_headers)
                text = payload.decode("utf-8", errors="replace")
                if exc.code in allow_status:
                    breaker.record_success()
                    return Response(status=exc.code, headers=response_headers,
                                    body=payload, url=url,
                                    elapsed_ms=int((time.monotonic() - started) * 1000))
                last_error = HttpError(
                    f"HTTP {exc.code} from {host}: {_short(text)}",
                    status=exc.code, body=text, url=url,
                )
                if exc.code == 429:
                    self._bump("rate_limited")
                if exc.code not in RETRYABLE_STATUS or attempt >= attempts:
                    self._bump("errors")
                    if exc.code >= 500 or exc.code == 429:
                        breaker.record_failure()
                    raise last_error
                delay = self._retry_after(response_headers) or self._backoff(attempt)
                LOG.info("Retrying %s after HTTP %s in %.1fs (attempt %d/%d)",
                         host, exc.code, delay, attempt + 1, attempts)
                self._bump("retries")
                time.sleep(delay)

            except (urllib.error.URLError, socket.timeout, ssl.SSLError,
                    ConnectionError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = HttpError(
                    f"Network error talking to {host}: {reason}", url=url)
                if attempt >= attempts:
                    self._bump("errors")
                    breaker.record_failure()
                    raise last_error
                delay = self._backoff(attempt)
                LOG.info("Retrying %s after network error in %.1fs (attempt %d/%d)",
                         host, delay, attempt + 1, attempts)
                self._bump("retries")
                time.sleep(delay)

        raise last_error or HttpError(f"Request to {url} failed", url=url)

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _backoff(attempt: int) -> float:
        base = min(8.0, 0.6 * (2 ** attempt))
        return base * (0.7 + random.random() * 0.6)

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return min(30.0, max(0.5, float(value)))
        except ValueError:
            return 5.0

    def host_health(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                host: {
                    "failures": breaker.failures,
                    "circuit_open": breaker.is_open,
                }
                for host, breaker in self._breakers.items()
            }


def _short(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
