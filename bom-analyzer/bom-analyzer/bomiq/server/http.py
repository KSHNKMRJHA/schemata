"""
Local HTTP layer: a tiny router on top of ``ThreadingHTTPServer``.

No web framework, on purpose. What the desktop app needs is a loopback-only
JSON API plus a handful of static files, and the standard library does that in
a few hundred lines with no install step and no version drift.

Security posture (this is a local tool, but the basics still matter):

* binds ``127.0.0.1`` by default and refuses non-loopback requests unless the
  user explicitly opts in with ``--host``
* rejects cross-origin requests and requires a session token for every
  mutating call, so a web page open in the user's browser cannot drive the app
* validates ``Host`` to block DNS-rebinding
* caps request bodies and sanitises static paths
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import socketserver
import threading
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from ..util import log
from ..version import APP_TITLE, __version__

LOG = log.get("server.http")

MAX_BODY = 220 * 1024 * 1024        # 220 MB, matching the reader's file cap
MAX_JSON_BODY = 8 * 1024 * 1024
STATIC_ROOT = Path(__file__).resolve().parent / "ui"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class HttpError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: Any
    body: bytes
    server: "AppServer"

    def param(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def int_param(self, name: str, default: int = 0) -> int:
        try:
            return int(self.param(name, str(default)))
        except (TypeError, ValueError):
            return default

    def bool_param(self, name: str, default: bool = False) -> bool:
        raw = self.param(name, "").lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on")

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        if len(self.body) > MAX_JSON_BODY:
            raise HttpError(413, "Request body is too large.")
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, f"Invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise HttpError(400, "Expected a JSON object.")
        return payload


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] | None = None
    filename: str = ""

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        return cls(status=status,
                   body=json.dumps(payload, default=_json_default).encode("utf-8"))

    @classmethod
    def text(cls, text: str, status: int = 200,
             content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status=status, body=text.encode("utf-8"),
                   content_type=content_type)

    @classmethod
    def file(cls, path: Path, download_name: str = "") -> "Response":
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or \
            "application/octet-stream"
        return cls(status=200, body=data, content_type=content_type,
                   filename=download_name or path.name)

    @classmethod
    def no_content(cls) -> "Response":
        return cls(status=204, body=b"")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "value"):          # enums
        return value.value
    return str(value)


Handler = Callable[[Request, dict[str, str]], Response]


class Router:
    """Pattern router. ``/api/jobs/{id}`` style placeholders."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler, bool]] = []

    def add(self, method: str, pattern: str, handler: Handler,
            public: bool = False) -> None:
        regex = re.compile(
            "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method.upper(), regex, handler, public))

    def get(self, pattern: str, public: bool = False):
        def decorator(func: Handler) -> Handler:
            self.add("GET", pattern, func, public)
            return func
        return decorator

    def post(self, pattern: str, public: bool = False):
        def decorator(func: Handler) -> Handler:
            self.add("POST", pattern, func, public)
            return func
        return decorator

    def delete(self, pattern: str, public: bool = False):
        def decorator(func: Handler) -> Handler:
            self.add("DELETE", pattern, func, public)
            return func
        return decorator

    def resolve(self, method: str, path: str
                ) -> tuple[Handler, dict[str, str], bool] | None:
        allowed: list[str] = []
        for route_method, regex, handler, public in self.routes:
            match = regex.match(path)
            if not match:
                continue
            if route_method != method.upper():
                allowed.append(route_method)
                continue
            return handler, match.groupdict(), public
        if allowed:
            raise HttpError(405, f"{method} not allowed on {path}",
                            {"allow": sorted(set(allowed))})
        return None


class AppServer:
    """Holds the router, the session token and whatever state handlers need."""

    def __init__(self, router: Router, state: dict[str, Any] | None = None,
                 host: str = "127.0.0.1", port: int = 8756,
                 token: str | None = None,
                 require_token: bool = True) -> None:
        self.router = router
        self.state: dict[str, Any] = state or {}
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.require_token = require_token
        self.httpd: socketserver.BaseServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------- #

    def serve_forever(self) -> None:
        handler = _make_handler(self)
        self.httpd = _ThreadingServer((self.host, self.port), handler)
        self.port = self.httpd.server_address[1]
        LOG.info("%s listening on http://%s:%s", APP_TITLE, self.host,
                 self.port)
        try:
            self.httpd.serve_forever(poll_interval=0.3)
        finally:
            self.httpd.server_close()

    def start_background(self) -> str:
        """Start the server on a daemon thread; returns the base URL."""
        handler = _make_handler(self)
        self.httpd = _ThreadingServer((self.host, self.port), handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.3},
            name="bomiq-http", daemon=True)
        self._thread.start()
        LOG.info("%s listening on %s", APP_TITLE, self.base_url)
        return self.base_url

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def app_url(self) -> str:
        return f"{self.base_url}/?t={urllib.parse.quote(self.token)}"

    # -- auth ------------------------------------------------------------- #

    def check_auth(self, request: Request, public: bool) -> None:
        if not self.require_token or public:
            return
        supplied = (
            request.headers.get("X-BOMIQ-Token")
            or request.param("t")
            or _bearer(request.headers.get("Authorization", ""))
        )
        if not supplied or not secrets.compare_digest(str(supplied), self.token):
            raise HttpError(
                401, "This request needs the session token. Open the app from "
                     "the link the launcher printed, or restart the app.")

    def check_origin(self, request: Request) -> None:
        """Block cross-site requests and DNS rebinding."""
        host_header = (request.headers.get("Host") or "").split(":")[0]
        if host_header and host_header not in ("localhost", "127.0.0.1",
                                               "[::1]", "::1", self.host):
            raise HttpError(
                403, f"Unexpected Host header {host_header!r}. Connect using "
                     f"http://{self.host}:{self.port} directly.")
        origin = request.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            if parsed.hostname not in ("localhost", "127.0.0.1", "::1",
                                       self.host):
                raise HttpError(403, "Cross-origin requests are not allowed.")


def _bearer(value: str) -> str:
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


class _ThreadingServer(socketserver.ThreadingMixIn,
                       http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Keep the accept queue short; this is a single-user local server.
    request_queue_size = 32


def _make_handler(app: AppServer) -> type[http.server.BaseHTTPRequestHandler]:

    class RequestHandler(http.server.BaseHTTPRequestHandler):
        server_version = f"BOM-IQ/{__version__}"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------- #

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        def _reject_remote(self) -> bool:
            if app.host not in ("127.0.0.1", "localhost", "::1"):
                return False
            try:
                address = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return True
            return not address.is_loopback

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpError(400, "Invalid Content-Length header.")
            if length < 0:
                raise HttpError(400, "Invalid Content-Length header.")
            if length > MAX_BODY:
                raise HttpError(
                    413, f"Upload is larger than "
                         f"{MAX_BODY // (1024 * 1024)} MB.")
            if length == 0:
                return b""
            chunks: list[bytes] = []
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def _send(self, response: Response) -> None:
            body = response.body or b""
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            if response.filename:
                safe = re.sub(r'[^\w.\- ]', "_", response.filename)
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{safe}"')
            for name, value in (response.headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    LOG.debug("Client disconnected before the response was "
                              "written.")

        def _error(self, status: int, message: str, detail: Any = None) -> None:
            payload: dict[str, Any] = {"error": message, "status": status}
            if detail is not None:
                payload["detail"] = detail
            self._send(Response.json(payload, status=status))

        # -- dispatch ---------------------------------------------------- #

        def _handle(self) -> None:
            if self._reject_remote():
                self._error(403, "This server only accepts connections from "
                                 "this computer.")
                return
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                body = self._read_body() if self.command in ("POST", "PUT",
                                                             "PATCH") else b""
            except HttpError as exc:
                self._error(exc.status, exc.message, exc.detail)
                return

            request = Request(method=self.command, path=path, query=query,
                              headers=self.headers, body=body, server=app)
            try:
                app.check_origin(request)
                resolved = app.router.resolve(self.command, path)
                if resolved is None:
                    if self.command in ("GET", "HEAD"):
                        self._send(_serve_static(path))
                        return
                    raise HttpError(404, f"No route for {path}")
                handler, params, public = resolved
                app.check_auth(request, public)
                response = handler(request, params)
                self._send(response)
            except HttpError as exc:
                self._error(exc.status, exc.message, exc.detail)
            except Exception as exc:  # pragma: no cover - defensive
                LOG.exception("Unhandled error serving %s", path)
                self._error(500, f"Internal error: {exc}")

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(Response(status=204, body=b"", headers={
                "Allow": "GET, POST, DELETE, OPTIONS",
            }))

    return RequestHandler


# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #

_STATIC_CACHE: dict[str, bytes] = {}


def _serve_static(path: str) -> Response:
    relative = "index.html" if path in ("", "/") else path.lstrip("/")
    # Prevent traversal: resolve and confirm the result stays under the root.
    candidate = (STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(STATIC_ROOT.resolve())
    except ValueError:
        raise HttpError(403, "Forbidden path.")
    if not candidate.is_file():
        raise HttpError(404, f"Not found: /{relative}")
    content_type = mimetypes.guess_type(candidate.name)[0] or "text/plain"
    if content_type.startswith(("text/", "application/javascript",
                                "application/json")):
        content_type += "; charset=utf-8"
    data = candidate.read_bytes()
    headers = {}
    if candidate.suffix in (".css", ".js", ".svg", ".woff2"):
        headers["Cache-Control"] = "no-cache"
    return Response(status=200, body=data, content_type=content_type,
                    headers=headers)


# --------------------------------------------------------------------------- #
# Multipart parsing
# --------------------------------------------------------------------------- #

@dataclass
class UploadedFile:
    field_name: str
    filename: str
    content_type: str
    data: bytes


def parse_multipart(body: bytes, content_type: str
                    ) -> tuple[dict[str, str], list[UploadedFile]]:
    """Parse ``multipart/form-data`` without the removed ``cgi`` module.

    Returns ``(fields, files)``. Robust against missing trailing CRLF and
    quoted boundaries; ignores parts without a name.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type or "",
                      re.IGNORECASE)
    if not match:
        raise HttpError(400, "Malformed multipart request: no boundary.")
    boundary = match.group(1).encode("utf-8")
    delimiter = b"--" + boundary
    fields: dict[str, str] = {}
    files: list[UploadedFile] = []

    segments = body.split(delimiter)
    for segment in segments[1:]:
        if segment[:2] == b"--":          # closing boundary
            break
        segment = segment.lstrip(b"\r\n")
        if not segment:
            continue
        head, _, payload = segment.partition(b"\r\n\r\n")
        if not _:
            continue
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        headers: dict[str, str] = {}
        for raw_line in head.split(b"\r\n"):
            line = raw_line.decode("utf-8", errors="replace")
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        field_name = name_match.group(1)
        file_match = re.search(r'filename\*?="?([^";]*)"?', disposition)
        if file_match and file_match.group(1):
            files.append(UploadedFile(
                field_name=field_name,
                filename=_safe_filename(file_match.group(1)),
                content_type=headers.get("content-type",
                                         "application/octet-stream"),
                data=payload,
            ))
        else:
            fields[field_name] = payload.decode("utf-8", errors="replace")
    return fields, files


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    """Reduce an arbitrary client-supplied name to a safe base filename."""
    return _safe_filename(name)


def _safe_filename(name: str) -> str:
    name = urllib.parse.unquote(name)
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE_NAME.sub("_", name).strip(". ")
    return name[:180] or "upload.bin"


def find_free_port(host: str = "127.0.0.1", preferred: int = 8756,
                   attempts: int = 24) -> int:
    """Return ``preferred`` if free, otherwise the next free port."""
    for offset in range(attempts):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def is_port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def iter_local_addresses() -> Iterable[str]:
    yield "127.0.0.1"
    yield "localhost"
