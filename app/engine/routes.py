"""FastAPI router exposing the full BOM-IQ REST API.

The SPA (served from /bom-iq) calls ``/api/*`` endpoints at the root level,
so this router is mounted WITHOUT a prefix on the FastAPI app.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.engine import bridge
from app.engine.bomiq.analysis import cost as cost_mod
from app.engine.bomiq.config import PROVIDER_SPECS
from app.engine.bomiq.core.errors import BomIQError, MappingError, ReadError
from app.engine.bomiq.core.models import BomAnalysis
from app.engine.bomiq.export import flat as flat_export
from app.engine.bomiq.export import html as html_export
from app.engine.bomiq.export import xlsx as xlsx_export
from app.engine.bomiq.ingest.detect import preview as region_preview
from app.engine.bomiq.ingest.detect import summarise_region
from app.engine.bomiq.ingest.mapping import describe_fields
from app.engine.bomiq.ingest.readers import describe_support
from app.engine.bomiq.ingest.validate import describe_rules
from app.engine.bomiq.util.text import clean
from app.engine.bomiq.version import APP_TITLE, __version__

router = APIRouter()

UPLOAD_TTL_SECONDS = 6 * 3600
MAX_UPLOADS = 20

# ---------------------------------------------------------------------------
# In-memory state for uploads and analyses (matches BOM-IQ's AppState pattern)
# ---------------------------------------------------------------------------
_uploads: dict[str, dict[str, Any]] = {}
_analyses: dict[str, dict[str, Any]] = {}
_started_at = time.time()


# ---------------------------------------------------------------------------
# Bring-your-own-key: per-request ephemeral provider credentials
# ---------------------------------------------------------------------------
# Public deployments hold no API keys on the server. Callers that own keys can
# send them for a single request via the ``X-Schemata-Keys`` header (base64url
# JSON ``{provider: {field: value}}``) and/or a ``credentials`` object in the
# JSON body. The engine consults them for that request only, then they are
# dropped — nothing is written to disk, keyring, logs or reports.

_BYOK_HEADER = "x-schemata-keys"
_MAX_BYOK_BYTES = 20000


def _byok_from_value(payload: Any) -> dict[str, dict[str, str]]:
    """Whitelist caller-supplied keys down to known providers and fields."""
    if not isinstance(payload, dict):
        raise HTTPException(
            400, "'credentials' must be an object mapping provider -> "
                 "{field: value}.")
    out: dict[str, dict[str, str]] = {}
    size = 0
    for provider_id, fields in payload.items():
        spec = PROVIDER_SPECS.get(str(provider_id))
        if spec is None:
            continue
        allowed = {c.key for c in spec.credentials}
        fields = fields if isinstance(fields, dict) else {}
        cleaned = {
            key: clean(value)
            for key, value in fields.items()
            if key in allowed and isinstance(value, str) and clean(value)
        }
        if not cleaned:
            continue
        size += sum(len(value) for value in cleaned.values())
        if size > _MAX_BYOK_BYTES:
            raise HTTPException(400, "Bring-your-own-key payload too large.")
        out[str(provider_id)] = cleaned
    return out


def _decode_byok_header(raw: str) -> dict[str, dict[str, str]]:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, "Invalid X-Schemata-Keys header.") from exc
    return _byok_from_value(payload)


def _byok_keys(request: Request, payload: Any = None
               ) -> dict[str, dict[str, str]] | None:
    """Ephemeral provider keys for this request (body then header, per-field)."""
    merged: dict[str, dict[str, str]] = {}
    if isinstance(payload, dict) and payload.get("credentials") is not None:
        merged.update(_byok_from_value(payload["credentials"]))
    raw = request.headers.get(_BYOK_HEADER)
    if raw:
        for provider_id, fields in _decode_byok_header(raw).items():
            merged.setdefault(provider_id, {}).update(fields)
    return merged or None


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w\-. ]", "_", (name or "").strip()) or "bom-analysis"
    return f"{stem[:60]}-{time.strftime('%Y%m%d-%H%M%S')}"


def _store_upload(filename: str, data: bytes) -> str:
    import uuid
    upload_id = uuid.uuid4().hex[:12]
    engine = bridge.get_engine()
    safe_name = re.sub(r"[^\w\-. ]", "_", filename)[:100] or "upload.csv"
    path = engine.config.upload_dir / f"{upload_id}-{safe_name}"
    path.write_bytes(data)
    _uploads[upload_id] = {
        "id": upload_id,
        "filename": safe_name,
        "path": path,
        "size": len(data),
        "created_at": time.time(),
    }
    return upload_id


def _get_upload(upload_id: str) -> dict[str, Any]:
    record = _uploads.get(upload_id)
    if record is None:
        raise HTTPException(404, "That upload has expired. Please re-upload the file.")
    return record


def _store_analysis(analysis: BomAnalysis) -> str:
    import uuid
    analysis_id = uuid.uuid4().hex[:12]
    _analyses[analysis_id] = analysis
    if len(_analyses) > 12:
        oldest = next(iter(_analyses))
        _analyses.pop(oldest, None)
    return analysis_id


def _get_analysis(analysis_id: str) -> BomAnalysis:
    analysis = _analyses.get(analysis_id)
    if analysis is not None:
        return analysis
    engine = bridge.get_engine()
    stored = engine.load_project(analysis_id)
    if stored is None:
        raise HTTPException(404, "That analysis is no longer in memory. Re-run it or open a saved project.")
    _analyses[analysis_id] = stored
    return stored


# ======================================================================== #
# Ping / Status / Bootstrap                                                #
# ======================================================================== #

@router.get("/api/ping")
async def ping():
    return {"ok": True, "app": APP_TITLE, "version": __version__}


@router.get("/api/status")
async def status():
    engine = bridge.get_engine()
    payload = engine.status()
    payload["uptime_seconds"] = int(time.time() - _started_at)
    return payload


@router.get("/api/bootstrap")
async def bootstrap():
    engine = bridge.get_engine()
    from app.engine.bomiq.export import flat as _flat
    return {
        "app": {
            "name": APP_TITLE, "version": __version__,
            "public": not bool(os.environ.get(
                "SCHEMATA_ACCESS_TOKEN", "").strip()),
        },
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
                           for key, label in _flat.iter_export_formats()],
        "projects": engine.projects.list(limit=25),
        "templates": engine.templates.all()[:25],
        "config": engine.config.summary(),
        "currencies": ["USD", "EUR", "GBP", "INR", "JPY", "CNY", "CAD", "AUD",
                       "CHF", "SGD", "SEK", "KRW", "TWD", "MXN", "BRL"],
    }


# ======================================================================== #
# Settings / Providers                                                      #
# ======================================================================== #

@router.get("/api/settings")
async def get_settings():
    engine = bridge.get_engine()
    return engine.config.settings.to_dict()


@router.post("/api/settings")
async def update_settings(request: Request):
    payload = await request.json()
    engine = bridge.get_engine()
    changed = engine.config.settings.update(payload)
    engine.config.save()
    engine.db.audit("settings.update", ", ".join(changed))
    return {
        "changed": changed,
        "settings": engine.config.settings.to_dict(),
        "enabled_providers": engine.config.enabled_providers(),
    }


@router.get("/api/providers")
async def get_providers():
    engine = bridge.get_engine()
    return engine.config.describe_providers()


@router.post("/api/providers/{provider_id}/credentials")
async def set_credentials(provider_id: str, request: Request):
    engine = bridge.get_engine()
    if provider_id not in PROVIDER_SPECS:
        raise HTTPException(404, f"Unknown provider {provider_id!r}")
    payload = await request.json()
    values = payload.get("credentials")
    if not isinstance(values, dict):
        raise HTTPException(400, "Expected a 'credentials' object.")
    allowed = {c.key for c in PROVIDER_SPECS[provider_id].credentials}
    cleaned = {key: clean(value) for key, value in values.items() if key in allowed}
    if not cleaned:
        expected = ", ".join(sorted(allowed))
        raise HTTPException(
            400,
            f"No recognised credential fields for {provider_id}. Expected: {expected}",
        )
    engine.config.credentials.set(provider_id, cleaned)
    engine.db.audit("credentials.update", provider_id)
    if payload.get("enable") and provider_id not in engine.config.settings.providers:
        engine.config.settings.providers.append(provider_id)
        engine.config.save()
    return {
        "provider": provider_id,
        "configured": engine.config.credentials.is_configured(provider_id),
        "providers": engine.config.describe_providers(),
    }


@router.delete("/api/providers/{provider_id}/credentials")
async def clear_credentials(provider_id: str):
    engine = bridge.get_engine()
    if provider_id not in PROVIDER_SPECS:
        raise HTTPException(404, f"Unknown provider {provider_id!r}")
    engine.config.credentials.clear(provider_id)
    engine.db.audit("credentials.clear", provider_id)
    return {"provider": provider_id, "configured": False}


@router.post("/api/providers/test")
async def test_providers(request: Request):
    engine = bridge.get_engine()
    payload = await request.json()
    ids = payload.get("providers")
    registry = engine.registry(
        ids if isinstance(ids, list) else None,
        credentials=_byok_keys(request, payload),
    )
    return {"results": registry.self_test()}


# ======================================================================== #
# Upload / Mapping / Analysis                                              #
# ======================================================================== #

@router.post("/api/upload")
async def upload(request: Request):
    content_type = request.headers.get("content-type", "")
    filename = "upload.csv"
    data = b""

    if "multipart/form-data" in content_type.lower():
        form = await request.form()
        files = form.getlist("files")
        if not files:
            up_file = form.get("file")
            files = [up_file] if up_file else []
        if not files:
            raise HTTPException(400, "No file was included in the upload.")
        upload_file = files[0]
        data = await upload_file.read()
        filename = upload_file.filename or filename
    else:
        data = await request.body()
        q_filename = request.query_params.get("filename")
        if q_filename:
            filename = q_filename

    if not data:
        raise HTTPException(400, "The uploaded file is empty.")

    upload_id = _store_upload(filename, data)
    engine = bridge.get_engine()

    try:
        result = engine.ingest_bytes(data, name=filename)
    except (ReadError, MappingError) as exc:
        return {
            "upload_id": upload_id, "filename": filename,
            "size": len(data), "ok": False,
            "error": str(exc), "error_kind": type(exc).__name__,
            "hint": "Open the column mapping panel to set the sheet, header row and columns manually.",
        }
    except BomIQError as exc:
        raise HTTPException(400, str(exc))

    _uploads[upload_id]["ingest"] = result
    return {
        "upload_id": upload_id, "filename": filename, "size": len(data),
        "ok": True, **result.to_dict(),
    }


@router.post("/api/remap")
async def remap(request: Request):
    """Re-ingest an upload with user-corrected mapping / sheet / header row."""
    payload = await request.json()
    record = _get_upload(clean(payload.get("upload_id")))
    engine = bridge.get_engine()

    forced = payload.get("mapping") or {}
    if not isinstance(forced, dict):
        raise HTTPException(400, "'mapping' must be an object of field -> column index.")
    cleaned_map: dict[str, int] = {}
    for key, value in forced.items():
        try:
            cleaned_map[str(key)] = int(value)
        except (TypeError, ValueError):
            continue

    options: dict[str, Any] = {"forced_mapping": cleaned_map}
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
        result = engine.ingest_bytes(data, name=record["filename"], **options)
    except BomIQError as exc:
        raise HTTPException(400, str(exc))
    record["ingest"] = result
    return {"upload_id": record["id"], "ok": True, **result.to_dict()}


@router.get("/api/upload/{upload_id}/preview")
async def upload_preview(upload_id: str, rows: int = Query(12)):
    record = _get_upload(upload_id)
    result = record.get("ingest")
    if result is None:
        raise HTTPException(409, "This upload has not been parsed yet.")
    return {
        "regions": [summarise_region(region) for region in result.regions],
        "preview": region_preview(result.chosen, rows=rows) if result.chosen else None,
        "mapping": result.mapping.to_dict(),
    }


@router.post("/api/analyse")
async def analyse(request: Request):
    payload = await request.json()
    engine = bridge.get_engine()

    if payload.get("settings"):
        engine.config.settings.update(payload["settings"])
        engine.config.save()

    providers = payload.get("providers")
    provider_ids = providers if isinstance(providers, list) else None

    upload_id = clean(payload.get("upload_id"))
    file_path = clean(payload.get("path"))

    if upload_id:
        record = _get_upload(upload_id)
        label = record["filename"]
        ingest_result = record.get("ingest")
        source_bytes = None if ingest_result else Path(record["path"]).read_bytes()
    elif file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"No file at {path}")
        label = path.name
        ingest_result = None
        source_bytes = path.read_bytes()
    else:
        raise HTTPException(400, "Provide either 'upload_id' or 'path'.")

    credentials = _byok_keys(request, payload)

    job_id = bridge.create_job(label, meta={"filename": label, "upload_id": upload_id or None})

    thread = threading.Thread(
        target=bridge.run_analysis_job,
        args=(job_id, ingest_result, source_bytes, label, provider_ids,
              credentials),
        name=f"schemata-bom-{job_id}",
        daemon=True,
    )
    thread.start()

    return {"job": bridge.get_job(job_id)}, 202


# ======================================================================== #
# Jobs                                                                      #
# ======================================================================== #

@router.get("/api/jobs")
async def list_jobs():
    with bridge._jobs_lock:
        return {"jobs": list(bridge._jobs.values())}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, events: int = Query(12)):
    job = bridge.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return {"job": {**job, "events": job.get("events", [])[-events:]}}


@router.get("/api/jobs/{job_id}/result")
async def get_job_result(job_id: str):
    job = bridge.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    if job["state"] == "error":
        raise HTTPException(500, detail={"message": job.get("error", "The job failed."), "kind": job.get("error_kind")})
    if job["state"] in ("queued", "running"):
        raise HTTPException(409, detail={"message": "The job is still running.", "progress": job.get("progress", 0)})
    if job["result"] is None:
        raise HTTPException(404, "That job produced no result.")
    return job["result"]


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    cancelled = bridge.cancel_job(job_id)
    return {"cancelled": cancelled}


# ======================================================================== #
# Export                                                                    #
# ======================================================================== #

EXPORTERS = {
    "xlsx": (xlsx_export.write_report, ".xlsx"),
    "csv": (flat_export.write_csv, ".csv"),
    "issues-csv": (flat_export.write_issues_csv, "-issues.csv"),
    "quote-csv": (flat_export.write_quote_request_csv, "-quote.csv"),
    "json": (lambda analysis, path: flat_export.write_json(analysis, path, full=True), ".json"),
    "json-flat": (lambda analysis, path: flat_export.write_json(analysis, path, full=False), "-flat.json"),
    "html": (html_export.write_html, ".html"),
}


@router.get("/api/analysis/{analysis_id}/export")
async def export_analysis(analysis_id: str, format: str = Query("xlsx"), inline: bool = Query(False)):
    analysis = _get_analysis(analysis_id)
    if format not in EXPORTERS:
        raise HTTPException(400, f"Unknown format {format!r}. Available: {', '.join(sorted(EXPORTERS))}")
    writer, suffix = EXPORTERS[format]
    engine = bridge.get_engine()
    stem = _safe_stem(analysis.bom.name or "bom-analysis")
    out_path = engine.config.export_dir / f"{stem}{suffix}"
    try:
        written = writer(analysis, out_path)
    except BomIQError as exc:
        raise HTTPException(500, str(exc))
    if inline and format == "html":
        return Response(content=written.read_bytes(), media_type="text/html; charset=utf-8")
    return FileResponse(written, filename=written.name)


@router.get("/api/analysis/{analysis_id}")
async def get_analysis(analysis_id: str):
    analysis = _get_analysis(analysis_id)
    return {"analysis": analysis.to_dict()}


@router.get("/api/analysis/{analysis_id}/price-comparison")
async def price_comparison(analysis_id: str):
    analysis = _get_analysis(analysis_id)
    engine = bridge.get_engine()
    comparison = cost_mod.compare_to_input_prices(analysis.results, analysis.summary.currency, engine.fx)
    return comparison


# ======================================================================== #
# Projects                                                                  #
# ======================================================================== #

@router.get("/api/projects")
async def list_projects(limit: int = Query(50)):
    engine = bridge.get_engine()
    return {"projects": engine.projects.list(limit=limit)}


@router.post("/api/projects")
async def save_project(request: Request):
    payload = await request.json()
    engine = bridge.get_engine()
    analysis_id = clean(payload.get("analysis_id"))
    analysis = _get_analysis(analysis_id)
    project_id = engine.save_project(
        analysis,
        name=clean(payload.get("name")),
        project_id=clean(payload.get("project_id")) or None,
    )
    return {"project_id": project_id, "projects": engine.projects.list(limit=25)}


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    engine = bridge.get_engine()
    analysis = engine.load_project(project_id)
    if analysis is None:
        raise HTTPException(404, "No such project.")
    analysis_id = _store_analysis(analysis)
    return {"analysis_id": analysis_id, "analysis": analysis.to_dict()}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    engine = bridge.get_engine()
    engine.projects.delete(project_id)
    return Response(status_code=204)


# ======================================================================== #
# Part lookup / search                                                      #
# ======================================================================== #

@router.get("/api/part")
async def get_part(request: Request, mpn: str = Query(""),
                   manufacturer: str = Query("")):
    if not mpn:
        raise HTTPException(400, "Provide an 'mpn' parameter.")
    engine = bridge.get_engine()
    registry = engine.registry(credentials=_byok_keys(request))
    result = registry.lookup(mpn, manufacturer)
    return {
        "query": {"mpn": mpn, "manufacturer": manufacturer},
        "found": result.found,
        **result.to_dict(),
        "provider_stats": registry.stats(),
    }


@router.post("/api/search")
async def search_parts(request: Request):
    payload = await request.json()
    query = clean(payload.get("query"))
    if not query:
        raise HTTPException(400, "Provide a 'query'.")
    try:
        limit = min(50, max(1, int(payload.get("limit") or 12)))
    except (TypeError, ValueError):
        limit = 12
    engine = bridge.get_engine()
    registry = engine.registry(credentials=_byok_keys(request, payload))
    parts = registry.search(query, limit=limit)
    return {
        "query": query,
        "count": len(parts),
        "parts": [part.to_dict() for part in parts],
    }


@router.post("/api/alternates")
async def find_alternates(request: Request):
    """On-demand alternate search for one part, used by the detail panel."""
    payload = await request.json()
    mpn = clean(payload.get("mpn"))
    if not mpn:
        raise HTTPException(400, "Provide an 'mpn'.")
    engine = bridge.get_engine()
    registry = engine.registry(credentials=_byok_keys(request, payload))
    lookup = registry.lookup(mpn, clean(payload.get("manufacturer")))
    if lookup.part is None:
        return {"mpn": mpn, "found": False, "alternates": []}
    from app.engine.bomiq.analysis.alternates import build_alternates
    from app.engine.bomiq.core.models import BomLine
    candidates = [(candidate, "similar") for candidate in registry.alternates(lookup.part, limit=12)]
    line = BomLine(line_no=0, mpn=mpn,
                   manufacturer=clean(payload.get("manufacturer")),
                   package=clean(payload.get("package")),
                   value=clean(payload.get("value")))
    try:
        quantity = max(0, int(payload.get("quantity") or 0))
    except (TypeError, ValueError):
        quantity = 0
    alternates = build_alternates(line, lookup.part, candidates, engine.config.settings, engine.fx, quantity)
    return {
        "mpn": mpn, "found": True,
        "part": lookup.part.to_dict(),
        "alternates": [a.to_dict() for a in alternates],
    }


# ======================================================================== #
# Reference data / maintenance                                             #
# ======================================================================== #

@router.get("/api/fields")
async def get_fields():
    return {"fields": describe_fields()}


@router.get("/api/rules")
async def get_rules():
    return {"rules": describe_rules()}


@router.get("/api/templates")
async def get_templates():
    engine = bridge.get_engine()
    return {"templates": engine.templates.all()}


@router.delete("/api/templates/{template_id}")
async def delete_template(template_id: str):
    engine = bridge.get_engine()
    engine.templates.delete(template_id)
    return Response(status_code=204)


@router.post("/api/cache/clear")
async def clear_cache():
    engine = bridge.get_engine()
    engine.db.clear_caches()
    engine.db.audit("cache.clear")
    return {"ok": True, "database": engine.db.stats()}


@router.post("/api/cache/prune")
async def prune_cache():
    engine = bridge.get_engine()
    removed = engine.db.prune()
    return {"removed": removed, "database": engine.db.stats()}


@router.get("/api/audit")
async def get_audit(limit: int = Query(80)):
    engine = bridge.get_engine()
    return {"entries": engine.db.recent_audit(limit=limit)}
