"""
The local REST API.

Every route is small and returns plain JSON. The UI is a single page that talks
to these endpoints; the CLI uses the engine directly, so nothing important
lives here that is not also reachable headlessly.

Route map
---------
``GET    /``                         the app (static)
``GET    /api/status``               version, paths, provider and cache state
``GET    /api/bootstrap``            everything the UI needs on first paint
``GET  / POST /api/settings``        read / update settings
``GET    /api/providers``            provider catalogue + credential state
``POST   /api/providers/{id}/credentials``
``POST   /api/providers/test``       live credential check
``POST   /api/upload``               upload a BOM, get the mapping preview
``POST   /api/remap``                re-ingest with an edited column mapping
``POST   /api/analyse``              start an analysis job
``GET    /api/jobs``                 list jobs
``GET    /api/jobs/{id}``            job progress
``GET    /api/jobs/{id}/result``     finished analysis
``POST   /api/jobs/{id}/cancel``
``GET    /api/analysis/{id}/export`` download a report
``GET    /api/projects``             saved analyses
``POST   /api/projects``             save the current analysis
``GET    /api/projects/{id}``
``DELETE /api/projects/{id}``
``GET    /api/part``                 single part lookup
``POST   /api/search``               keyword part search
``GET    /api/fields`` ``/api/rules`` ``/api/templates``
``POST   /api/cache/clear``
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from ..analysis import cost as cost_mod
from ..config import PROVIDER_SPECS, Config
from ..core.errors import BomIQError, MappingError, ReadError
from ..core.models import BomAnalysis
from ..engine import Engine
from ..export import flat, html as html_export, xlsx as xlsx_export
from ..ingest.detect import preview as region_preview, summarise_region
from ..ingest.mapping import describe_fields
from ..ingest.readers import describe_support
from ..ingest.validate import describe_rules
from ..util import log
from ..util.text import clean
from ..version import APP_TITLE, __version__
from .http import (
    AppServer, HttpError, Request, Response, Router, find_free_port,
    parse_multipart, safe_filename,
)
from .jobs import JobManager

LOG = log.get("server.app")

router = Router()

UPLOAD_TTL_SECONDS = 6 * 3600
MAX_UPLOADS = 20


class AppState:
    """Server-side state: the engine, jobs, uploads and analyses in memory."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.jobs = JobManager()
        self.uploads: dict[str, dict[str, Any]] = {}
        self.analyses: dict[str, BomAnalysis] = {}
        self.started_at = time.time()

    # -- uploads ---------------------------------------------------------- #

    def store_upload(self, filename: str, data: bytes) -> str:
        upload_id = uuid.uuid4().hex[:12]
        # The name may come from a query parameter, so it is sanitised here as
        # well as in the multipart parser: a path separator or a 400-character
        # name would otherwise turn into an OSError and a 500.
        filename = safe_filename(filename)
        path = self.engine.config.upload_dir / f"{upload_id}-{filename}"
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise HttpError(
                500, f"Could not save the upload: {exc.strerror or exc}. "
                     f"Check that {self.engine.config.upload_dir} is "
                     f"writable.") from exc
        self.uploads[upload_id] = {
            "id": upload_id, "filename": filename, "path": path,
            "size": len(data), "created_at": time.time(),
        }
        self._reap_uploads()
        return upload_id

    def upload(self, upload_id: str) -> dict[str, Any]:
        record = self.uploads.get(upload_id)
        if record is None:
            raise HttpError(404, "That upload has expired. Please re-upload "
                                 "the file.")
        return record

    def _reap_uploads(self) -> None:
        now = time.time()
        stale = [key for key, record in self.uploads.items()
                 if now - record["created_at"] > UPLOAD_TTL_SECONDS]
        ordered = sorted(self.uploads.items(),
                         key=lambda item: item[1]["created_at"])
        while len(ordered) - len(stale) > MAX_UPLOADS:
            stale.append(ordered.pop(0)[0])
        for key in set(stale):
            record = self.uploads.pop(key, None)
            if record:
                try:
                    Path(record["path"]).unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    pass

    # -- analyses --------------------------------------------------------- #

    def store_analysis(self, analysis: BomAnalysis) -> str:
        analysis_id = uuid.uuid4().hex[:12]
        self.analyses[analysis_id] = analysis
        if len(self.analyses) > 12:
            oldest = next(iter(self.analyses))
            self.analyses.pop(oldest, None)
        return analysis_id

    def analysis(self, analysis_id: str) -> BomAnalysis:
        analysis = self.analyses.get(analysis_id)
        if analysis is None:
            stored = self.engine.load_project(analysis_id)
            if stored is None:
                raise HttpError(404, "That analysis is no longer in memory. "
                                     "Re-run it or open the saved project.")
            self.analyses[analysis_id] = stored
            return stored
        return analysis


def _state(request: Request) -> AppState:
    return request.server.state["app"]


# --------------------------------------------------------------------------- #
# Status and bootstrap
# --------------------------------------------------------------------------- #

@router.get("/api/ping", public=True)
def ping(request: Request, _: dict[str, str]) -> Response:
    """Unauthenticated liveness probe used by the desktop launcher."""
    return Response.json({"ok": True, "app": APP_TITLE,
                          "version": __version__})


@router.get("/api/status")
def status(request: Request, _: dict[str, str]) -> Response:
    state = _state(request)
    payload = state.engine.status()
    payload["uptime_seconds"] = int(time.time() - state.started_at)
    payload["jobs"] = state.jobs.list()
    payload["uploads"] = [
        {"id": r["id"], "filename": r["filename"], "size": r["size"]}
        for r in state.uploads.values()
    ]
    return Response.json(payload)


@router.get("/api/bootstrap")
def bootstrap(request: Request, _: dict[str, str]) -> Response:
    """One call that gives the UI everything it needs to render."""
    state = _state(request)
    engine = state.engine
    return Response.json({
        "app": {"name": APP_TITLE, "version": __version__},
        "settings": engine.config.settings.to_dict(),
        "providers": engine.config.describe_providers(),
        "provider_specs": {
            spec.id: {
                "id": spec.id, "name": spec.name, "kind": spec.kind,
                "notes": spec.notes, "signup_url": spec.signup_url,
                "docs_url": spec.docs_url,
                "credentials": [
                    {"key": c.key, "label": c.label, "secret": c.secret,
                     "required": c.required, "help": c.help}
                    for c in spec.credentials
                ],
            }
            for spec in PROVIDER_SPECS.values()
        },
        "fields": describe_fields(),
        "rules": describe_rules(),
        "file_support": describe_support(),
        "export_formats": [{"id": key, "label": label}
                           for key, label in flat.iter_export_formats()],
        "projects": engine.projects.list(limit=25),
        "templates": engine.templates.all()[:25],
        "config": engine.config.summary(),
        "currencies": ["USD", "EUR", "GBP", "INR", "JPY", "CNY", "CAD", "AUD",
                       "CHF", "SGD", "SEK", "KRW", "TWD", "MXN", "BRL"],
    })


# --------------------------------------------------------------------------- #
# Settings and providers
# --------------------------------------------------------------------------- #

@router.get("/api/settings")
def get_settings(request: Request, _: dict[str, str]) -> Response:
    return Response.json(_state(request).engine.config.settings.to_dict())


@router.post("/api/settings")
def update_settings(request: Request, _: dict[str, str]) -> Response:
    engine = _state(request).engine
    payload = request.json()
    changed = engine.config.settings.update(payload)
    engine.config.save()
    engine.db.audit("settings.update", ", ".join(changed))
    return Response.json({
        "changed": changed,
        "settings": engine.config.settings.to_dict(),
        "enabled_providers": engine.config.enabled_providers(),
    })


@router.get("/api/providers")
def get_providers(request: Request, _: dict[str, str]) -> Response:
    return Response.json(_state(request).engine.config.describe_providers())


@router.post("/api/providers/{provider_id}/credentials")
def set_credentials(request: Request, params: dict[str, str]) -> Response:
    engine = _state(request).engine
    provider_id = params["provider_id"]
    if provider_id not in PROVIDER_SPECS:
        raise HttpError(404, f"Unknown provider {provider_id!r}")
    payload = request.json()
    values = payload.get("credentials")
    if not isinstance(values, dict):
        raise HttpError(400, "Expected a 'credentials' object.")
    allowed = {c.key for c in PROVIDER_SPECS[provider_id].credentials}
    cleaned = {key: clean(value) for key, value in values.items()
               if key in allowed}
    if not cleaned:
        raise HttpError(400, f"No recognised credential fields for "
                             f"{provider_id}. Expected: "
                             f"{', '.join(sorted(allowed))}")
    engine.config.credentials.set(provider_id, cleaned)
    engine.db.audit("credentials.update", provider_id)
    if payload.get("enable") and provider_id not in \
            engine.config.settings.providers:
        engine.config.settings.providers.append(provider_id)
        engine.config.save()
    return Response.json({
        "provider": provider_id,
        "configured": engine.config.credentials.is_configured(provider_id),
        "providers": engine.config.describe_providers(),
    })


@router.delete("/api/providers/{provider_id}/credentials")
def clear_credentials(request: Request, params: dict[str, str]) -> Response:
    engine = _state(request).engine
    provider_id = params["provider_id"]
    if provider_id not in PROVIDER_SPECS:
        raise HttpError(404, f"Unknown provider {provider_id!r}")
    engine.config.credentials.clear(provider_id)
    engine.db.audit("credentials.clear", provider_id)
    return Response.json({"provider": provider_id, "configured": False})


@router.post("/api/providers/test")
def test_providers(request: Request, _: dict[str, str]) -> Response:
    engine = _state(request).engine
    payload = request.json()
    ids = payload.get("providers")
    registry = engine.registry(ids if isinstance(ids, list) else None)
    return Response.json({"results": registry.self_test()})


# --------------------------------------------------------------------------- #
# Upload, mapping, analysis
# --------------------------------------------------------------------------- #

@router.post("/api/upload")
def upload(request: Request, _: dict[str, str]) -> Response:
    state = _state(request)
    content_type = request.headers.get("Content-Type", "")
    filename = request.param("filename") or "upload.csv"
    data = b""

    if "multipart/form-data" in content_type.lower():
        _, files = parse_multipart(request.body, content_type)
        if not files:
            raise HttpError(400, "No file was included in the upload.")
        data = files[0].data
        filename = files[0].filename or filename
    else:
        data = request.body
    if not data:
        raise HttpError(400, "The uploaded file is empty.")

    upload_id = state.store_upload(filename, data)
    try:
        result = state.engine.ingest_bytes(data, name=filename)
    except (ReadError, MappingError) as exc:
        # Keep the upload so the user can fix the mapping by hand.
        return Response.json({
            "upload_id": upload_id, "filename": filename,
            "size": len(data), "ok": False,
            "error": str(exc), "error_kind": type(exc).__name__,
            "hint": "Open the column mapping panel to set the sheet, header "
                    "row and columns manually.",
        }, status=200)
    except BomIQError as exc:
        raise HttpError(400, str(exc)) from exc

    state.uploads[upload_id]["ingest"] = result
    return Response.json({
        "upload_id": upload_id, "filename": filename, "size": len(data),
        "ok": True, **result.to_dict(),
    })


@router.post("/api/remap")
def remap(request: Request, _: dict[str, str]) -> Response:
    """Re-ingest an upload with user-corrected mapping / sheet / header row."""
    state = _state(request)
    payload = request.json()
    record = state.upload(clean(payload.get("upload_id")))
    forced = payload.get("mapping") or {}
    if not isinstance(forced, dict):
        raise HttpError(400, "'mapping' must be an object of field -> column "
                             "index.")
    cleaned: dict[str, int] = {}
    for key, value in forced.items():
        try:
            cleaned[str(key)] = int(value)
        except (TypeError, ValueError):
            continue

    options: dict[str, Any] = {"forced_mapping": cleaned}
    if payload.get("sheet_name"):
        options["sheet_name"] = clean(payload["sheet_name"])
    if payload.get("header_row"):
        try:
            options["header_row"] = int(payload["header_row"])
        except (TypeError, ValueError):
            pass
    for key in ("merge_duplicate_mpns", "expand_ref_ranges",
                "treat_blank_qty_as_one", "combine_matching_sheets",
                "learn_templates"):
        if key in payload:
            options[key] = bool(payload[key])

    data = Path(record["path"]).read_bytes()
    try:
        result = state.engine.ingest_bytes(data, name=record["filename"],
                                           **options)
    except BomIQError as exc:
        raise HttpError(400, str(exc)) from exc
    record["ingest"] = result
    return Response.json({"upload_id": record["id"], "ok": True,
                          **result.to_dict()})


@router.get("/api/upload/{upload_id}/preview")
def upload_preview(request: Request, params: dict[str, str]) -> Response:
    state = _state(request)
    record = state.upload(params["upload_id"])
    result = record.get("ingest")
    if result is None:
        raise HttpError(409, "This upload has not been parsed yet.")
    rows = request.int_param("rows", 12)
    return Response.json({
        "regions": [summarise_region(region) for region in result.regions],
        "preview": region_preview(result.chosen, rows=rows)
        if result.chosen else None,
        "mapping": result.mapping.to_dict(),
    })


@router.post("/api/analyse")
def analyse(request: Request, _: dict[str, str]) -> Response:
    state = _state(request)
    payload = request.json()
    engine = state.engine

    if payload.get("settings"):
        engine.config.settings.update(payload["settings"])
        engine.config.save()

    providers = payload.get("providers")
    provider_ids = providers if isinstance(providers, list) else None

    upload_id = clean(payload.get("upload_id"))
    file_path = clean(payload.get("path"))

    if upload_id:
        record = state.upload(upload_id)
        label = record["filename"]
        ingest_result = record.get("ingest")
        source_bytes = None if ingest_result else Path(record["path"]).read_bytes()
    elif file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise HttpError(400, f"No file at {path}")
        label = path.name
        ingest_result = None
        source_bytes = path.read_bytes()
    else:
        raise HttpError(400, "Provide either 'upload_id' or 'path'.")

    def work(job: Any) -> dict[str, Any]:
        progress = state.jobs.progress_callback(job)
        if ingest_result is not None:
            analysis = engine.analyse_bom(
                ingest_result.bom, progress=progress, cancel=job.cancel,
                provider_ids=provider_ids)
        else:
            analysis = engine.analyse_bytes(
                source_bytes or b"", name=label, progress=progress,
                cancel=job.cancel, provider_ids=provider_ids)
        analysis_id = state.store_analysis(analysis)
        job.meta["analysis_id"] = analysis_id
        return {"analysis_id": analysis_id, "analysis": analysis.to_dict()}

    job = state.jobs.submit("analyse", label, work,
                            meta={"filename": label,
                                  "upload_id": upload_id or None})
    return Response.json({"job": job.to_dict()}, status=202)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@router.get("/api/jobs")
def list_jobs(request: Request, _: dict[str, str]) -> Response:
    return Response.json({"jobs": _state(request).jobs.list()})


@router.get("/api/jobs/{job_id}")
def get_job(request: Request, params: dict[str, str]) -> Response:
    job = _state(request).jobs.get(params["job_id"])
    if job is None:
        raise HttpError(404, "No such job.")
    return Response.json({"job": job.to_dict(
        include_events=request.int_param("events", 12))})


@router.get("/api/jobs/{job_id}/result")
def get_job_result(request: Request, params: dict[str, str]) -> Response:
    job = _state(request).jobs.get(params["job_id"])
    if job is None:
        raise HttpError(404, "No such job.")
    if job.state == "error":
        raise HttpError(500, job.error or "The job failed.",
                        {"kind": job.error_kind})
    if job.state in ("queued", "running"):
        raise HttpError(409, "The job is still running.",
                        {"progress": job.progress})
    if job.result is None:
        raise HttpError(404, "That job produced no result.")
    return Response.json(job.result)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(request: Request, params: dict[str, str]) -> Response:
    cancelled = _state(request).jobs.cancel(params["job_id"])
    return Response.json({"cancelled": cancelled})


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

EXPORTERS = {
    "xlsx": (xlsx_export.write_report, ".xlsx"),
    "csv": (flat.write_csv, ".csv"),
    "issues-csv": (flat.write_issues_csv, "-issues.csv"),
    "quote-csv": (flat.write_quote_request_csv, "-quote.csv"),
    "json": (lambda analysis, path: flat.write_json(analysis, path, full=True),
             ".json"),
    "json-flat": (lambda analysis, path: flat.write_json(analysis, path,
                                                         full=False),
                  "-flat.json"),
    "html": (html_export.write_html, ".html"),
}


@router.get("/api/analysis/{analysis_id}/export")
def export_analysis(request: Request, params: dict[str, str]) -> Response:
    state = _state(request)
    analysis = state.analysis(params["analysis_id"])
    fmt = request.param("format", "xlsx")
    if fmt not in EXPORTERS:
        raise HttpError(400, f"Unknown format {fmt!r}. Available: "
                             f"{', '.join(sorted(EXPORTERS))}")
    writer, suffix = EXPORTERS[fmt]
    stem = _safe_stem(analysis.bom.name or "bom-analysis")
    out_path = state.engine.config.export_dir / f"{stem}{suffix}"
    try:
        written = writer(analysis, out_path)
    except BomIQError as exc:
        raise HttpError(500, str(exc)) from exc
    if request.bool_param("inline") and fmt == "html":
        return Response(status=200, body=written.read_bytes(),
                        content_type="text/html; charset=utf-8")
    return Response.file(written, download_name=written.name)


@router.get("/api/analysis/{analysis_id}")
def get_analysis(request: Request, params: dict[str, str]) -> Response:
    analysis = _state(request).analysis(params["analysis_id"])
    return Response.json({"analysis": analysis.to_dict()})


@router.get("/api/analysis/{analysis_id}/price-comparison")
def price_comparison(request: Request, params: dict[str, str]) -> Response:
    state = _state(request)
    analysis = state.analysis(params["analysis_id"])
    comparison = cost_mod.compare_to_input_prices(
        analysis.results, analysis.summary.currency, state.engine.fx)
    return Response.json(comparison)


def _safe_stem(name: str) -> str:
    import re

    stem = re.sub(r"[^\w\-. ]", "_", clean(name)).strip() or "bom-analysis"
    return f"{stem[:60]}-{time.strftime('%Y%m%d-%H%M%S')}"


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

@router.get("/api/projects")
def list_projects(request: Request, _: dict[str, str]) -> Response:
    engine = _state(request).engine
    return Response.json({"projects": engine.projects.list(
        limit=request.int_param("limit", 50))})


@router.post("/api/projects")
def save_project(request: Request, _: dict[str, str]) -> Response:
    state = _state(request)
    payload = request.json()
    analysis = state.analysis(clean(payload.get("analysis_id")))
    project_id = state.engine.save_project(
        analysis, name=clean(payload.get("name")),
        project_id=clean(payload.get("project_id")) or None)
    return Response.json({"project_id": project_id,
                          "projects": state.engine.projects.list(limit=25)})


@router.get("/api/projects/{project_id}")
def get_project(request: Request, params: dict[str, str]) -> Response:
    state = _state(request)
    analysis = state.engine.load_project(params["project_id"])
    if analysis is None:
        raise HttpError(404, "No such project.")
    analysis_id = state.store_analysis(analysis)
    return Response.json({"analysis_id": analysis_id,
                          "analysis": analysis.to_dict()})


@router.delete("/api/projects/{project_id}")
def delete_project(request: Request, params: dict[str, str]) -> Response:
    _state(request).engine.projects.delete(params["project_id"])
    return Response.no_content()


# --------------------------------------------------------------------------- #
# Part lookup and search
# --------------------------------------------------------------------------- #

@router.get("/api/part")
def get_part(request: Request, _: dict[str, str]) -> Response:
    state = _state(request)
    mpn = clean(request.param("mpn"))
    if not mpn:
        raise HttpError(400, "Provide an 'mpn' parameter.")
    manufacturer = clean(request.param("manufacturer"))
    registry = state.engine.registry()
    result = registry.lookup(mpn, manufacturer)
    return Response.json({
        "query": {"mpn": mpn, "manufacturer": manufacturer},
        "found": result.found,
        **result.to_dict(),
        "provider_stats": registry.stats(),
    })


@router.post("/api/search")
def search_parts(request: Request, _: dict[str, str]) -> Response:
    state = _state(request)
    payload = request.json()
    query = clean(payload.get("query"))
    if not query:
        raise HttpError(400, "Provide a 'query'.")
    try:
        limit = min(50, max(1, int(payload.get("limit") or 12)))
    except (TypeError, ValueError):
        limit = 12
    registry = state.engine.registry()
    parts = registry.search(query, limit=limit)
    return Response.json({
        "query": query,
        "count": len(parts),
        "parts": [part.to_dict() for part in parts],
    })


@router.post("/api/alternates")
def find_alternates(request: Request, _: dict[str, str]) -> Response:
    """On-demand alternate search for one part, used by the detail panel."""
    state = _state(request)
    payload = request.json()
    mpn = clean(payload.get("mpn"))
    if not mpn:
        raise HttpError(400, "Provide an 'mpn'.")
    registry = state.engine.registry()
    lookup = registry.lookup(mpn, clean(payload.get("manufacturer")))
    if lookup.part is None:
        return Response.json({"mpn": mpn, "found": False, "alternates": []})
    candidates = [(candidate, "similar")
                  for candidate in registry.alternates(lookup.part, limit=12)]
    from ..analysis.alternates import build_alternates
    from ..core.models import BomLine

    line = BomLine(line_no=0, mpn=mpn,
                   manufacturer=clean(payload.get("manufacturer")),
                   package=clean(payload.get("package")),
                   value=clean(payload.get("value")))
    try:
        quantity = max(0, int(payload.get("quantity") or 0))
    except (TypeError, ValueError):
        quantity = 0
    alternates = build_alternates(
        line, lookup.part, candidates, state.engine.config.settings,
        state.engine.fx, quantity)
    return Response.json({
        "mpn": mpn, "found": True,
        "part": lookup.part.to_dict(),
        "alternates": [alternate.to_dict() for alternate in alternates],
    })


# --------------------------------------------------------------------------- #
# Reference data and maintenance
# --------------------------------------------------------------------------- #

@router.get("/api/fields")
def get_fields(request: Request, _: dict[str, str]) -> Response:
    return Response.json({"fields": describe_fields()})


@router.get("/api/rules")
def get_rules(request: Request, _: dict[str, str]) -> Response:
    return Response.json({"rules": describe_rules()})


@router.get("/api/templates")
def get_templates(request: Request, _: dict[str, str]) -> Response:
    return Response.json({
        "templates": _state(request).engine.templates.all()})


@router.delete("/api/templates/{template_id}")
def delete_template(request: Request, params: dict[str, str]) -> Response:
    _state(request).engine.templates.delete(params["template_id"])
    return Response.no_content()


@router.post("/api/cache/clear")
def clear_cache(request: Request, _: dict[str, str]) -> Response:
    engine = _state(request).engine
    engine.db.clear_caches()
    engine.db.audit("cache.clear")
    return Response.json({"ok": True, "database": engine.db.stats()})


@router.post("/api/cache/prune")
def prune_cache(request: Request, _: dict[str, str]) -> Response:
    engine = _state(request).engine
    removed = engine.db.prune()
    return Response.json({"removed": removed, "database": engine.db.stats()})


@router.get("/api/audit")
def get_audit(request: Request, _: dict[str, str]) -> Response:
    return Response.json({
        "entries": _state(request).engine.db.recent_audit(
            limit=request.int_param("limit", 80))})


@router.post("/api/shutdown")
def shutdown(request: Request, _: dict[str, str]) -> Response:
    """Let the desktop shell stop the server cleanly."""
    server = request.server

    def stop() -> None:
        time.sleep(0.25)
        server.stop()

    import threading

    threading.Thread(target=stop, daemon=True).start()
    return Response.json({"stopping": True})


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def create_server(config: Config | None = None, host: str = "127.0.0.1",
                  port: int = 0, token: str | None = None,
                  require_token: bool = True,
                  engine: Engine | None = None) -> AppServer:
    """Build (but do not start) the local server."""
    engine = engine or Engine(config)
    chosen_port = port or find_free_port(host, engine.config.settings.port)
    server = AppServer(router=router, host=host, port=chosen_port,
                       token=token, require_token=require_token)
    server.state["app"] = AppState(engine)
    server.state["engine"] = engine
    return server


def serve(config: Config | None = None, host: str = "127.0.0.1",
          port: int = 0, open_browser: bool = False,
          require_token: bool = True) -> None:
    """Run the server in the foreground (used by ``bomiq serve``)."""
    server = create_server(config, host=host, port=port,
                           require_token=require_token)
    url = server.app_url
    print(f"{APP_TITLE} v{__version__}")
    print(f"  UI:     {url}")
    print(f"  API:    {server.base_url}/api/status")
    print(f"  Token:  {server.token}")
    print("  Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.state["engine"].close()
