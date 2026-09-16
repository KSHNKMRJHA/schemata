"""FastAPI application entrypoint + UI routes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import reports, schemas
from app.config import DATA_DIR, UI_DIR
from app.db import init_db
from app.engine.parts_bridge import source_status
from app.engine.routes import router as bom_router
from app.history import build_history
from app.lifecycle import (
    allowed_transitions,
    status_color,
    status_description,
)
from app.ratelimit import api_rate_limit
from app.service import get_part_report, recent_parts

logger = logging.getLogger(__name__)

_DATA_DIR = DATA_DIR

init_db()

@asynccontextmanager
async def _lifespan(_: FastAPI):
    """Validate required directories and database availability on startup."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - a broken DB must fail startup loudly
        logger.critical("Database initialization failed: %s", exc)
        raise
    yield


app = FastAPI(title="Schemata", version="2.2.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")

# BOM analysis engine API + eligible single-page app (Schemata).
app.include_router(bom_router)
app.mount("/bom-iq", StaticFiles(directory=str(UI_DIR / "bom-iq"), html=True),
          name="bom-iq")

_ACCESS_TOKEN_ENV = "SCHEMATA_ACCESS_TOKEN"
_ACCESS_COOKIE = "schemata_access"
_RAW_TOKEN_RE = re.compile(r"(?:^|&)token=([^&]*)")


def _access_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.middleware("http")
async def _require_access_token(request: Request, call_next):
    """Gate every route when SCHEMATA_ACCESS_TOKEN is set (online deployments).

    Local use (no env var) is unaffected. Unauthenticated *page* requests are
    redirected to the login screen; ``/api/*`` requests receive the usual 401
    JSON so the SPA front-end can detect them.

    Tokens often contain ``+`` / ``=`` (e.g. base64). Because query parsing
    decodes ``+`` as a space, both the decoded value and the raw URL segment
    are accepted, so a token can be pasted verbatim into the login form.
    """
    token = os.environ.get(_ACCESS_TOKEN_ENV, "").strip()
    if not token:
        return await call_next(request)

    path = request.url.path
    if path in ("/healthz", "/login") or path.startswith("/static/"):
        return await call_next(request)

    digest = _access_token_hash(token)
    candidates: list[str] = []
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        candidates.append(auth[7:].strip())
    decoded = request.query_params.get("token", "").strip()
    if decoded:
        candidates.append(decoded)
    match = _RAW_TOKEN_RE.search(request.url.query or "")
    if match:
        raw = match.group(1)
        if raw and raw != decoded:
            candidates.append(raw)
    for candidate in candidates:
        if secrets.compare_digest(candidate, token):
            response = await call_next(request)
            response.set_cookie(
                _ACCESS_COOKIE, digest,
                max_age=7 * 86400, httponly=True, samesite="strict",
                secure=request.url.scheme == "https",
            )
            return response
    if secrets.compare_digest(request.cookies.get(_ACCESS_COOKIE, ""), digest):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return RedirectResponse(
        f"/login?next={quote(path, safe='')}", status_code=302,
    )


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_get(next: str = "/parts"):
    if not os.environ.get(_ACCESS_TOKEN_ENV, "").strip():
        return RedirectResponse("/parts", status_code=302)
    return _render("login.html", {"next": next, "error": None})


@app.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_post(request: Request, next: str = "/parts"):
    correct = os.environ.get(_ACCESS_TOKEN_ENV, "").strip()
    if not correct:
        return RedirectResponse("/parts", status_code=302)
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8", errors="ignore")
    candidates: list[str] = []
    form = await request.form()
    decoded = (form.get("token") or "").strip()
    if decoded:
        candidates.append(decoded)
    raw_match = _RAW_TOKEN_RE.search(body_str)
    if raw_match:
        raw = raw_match.group(1)
        if raw and raw != decoded:
            candidates.append(raw)
    for candidate in candidates:
        if secrets.compare_digest(candidate, correct):
            digest = _access_token_hash(correct)
            response = RedirectResponse(next, status_code=302)
            response.set_cookie(
                _ACCESS_COOKIE, digest,
                max_age=7 * 86400, httponly=True, samesite="strict",
                secure=request.url.scheme == "https",
            )
            return response
    return _render("login.html", {"next": next, "error": "Invalid token — try again."})

_env = Environment(
    loader=FileSystemLoader(str(UI_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
_env.filters["status_color"] = status_color
_env.filters["status_desc"] = status_description
_env.filters["datetime"] = lambda dt: dt.strftime("%Y-%m-%d") if dt else "—"


def _render(template: str, ctx: dict | None = None) -> HTMLResponse:
    ctx = dict(ctx or {})
    sources = source_status()
    ctx.setdefault("live_sources", sources["live"])
    ctx.setdefault("mock_sources", sources["mock"])
    return HTMLResponse(_env.get_template(template).render(**ctx))


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/parts")


@app.get("/parts", response_class=HTMLResponse)
def parts_page():
    parts = recent_parts(40)
    return _render("search.html", {"recent": parts, "mpn_q": "", "mfr_q": ""})


@app.get("/parts/search")
async def parts_search(mpn: str, manufacturer: str | None = None, force: bool = False):
    try:
        report = await get_part_report(mpn, manufacturer, force=force)
    except Exception as exc:  # noqa: BLE001 - surface errors to the user
        logger.warning("Search failed for MPN=%s manufacturer=%s: %s", mpn, manufacturer, exc)
        return _render(
            "search.html",
            {
                "error": str(exc),
                "recent": recent_parts(40),
                "mpn_q": mpn,
                "mfr_q": manufacturer or "",
            },
        )
    return part_page_render(report)


@app.get("/parts/{mpn}", response_class=HTMLResponse)
async def part_detail(mpn: str, force: bool = False, manufacturer: str | None = None):
    try:
        report = await get_part_report(mpn, manufacturer, force=force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Detail lookup failed for MPN=%s: %s", mpn, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return part_page_render(report)


def part_page_render(report: schemas.PartReportOut) -> HTMLResponse:
    c = report.component
    history = build_history([schemas_snap_to_model(s) for s in c.snapshots]) if c.snapshots else None
    series = history.series if history else []
    trends = [t.__dict__ for t in (history.trends if history else [])]
    return _render(
        "part.html",
        {
            "report": report,
            "component": c,
            "history": {
                "series": series,
                "trends": trends,
                "latest_total_stock": history.latest_total_stock if history else None,
                "latest_min_price": history.latest_min_price if history else None,
                "price_delta_pct": history.price_delta_pct if history else None,
            },
            "history_json": json.dumps(series, default=str),
            "transitions": allowed_transitions(c.lifecycle_status),
            "lifecycle_tag": status_color(c.lifecycle_status),
        },
    )


def schemas_snap_to_model(s: schemas.SnapshotOut):
    """Adapter shim: build a lightweight snapshot with the same fields history needs."""
    from app.models import AvailabilitySnapshot

    snap = AvailabilitySnapshot()
    snap.distributor = s.distributor
    snap.available = s.available
    snap.available_qty = s.available_qty
    snap.price_breaks = [dict(b) for b in s.price_breaks]
    snap.moq = s.moq
    snap.lead_time = s.lead_time
    snap.checked_at = s.checked_at
    return snap


@app.get("/api/parts", response_model=list[schemas.ComponentOut])
def api_recent(limit: int = Query(50, ge=1, le=500), _: None = Depends(api_rate_limit)):
    from app.service import to_component_out

    return [to_component_out(c) for c in recent_parts(limit)]


@app.get("/api/parts/{mpn}", response_model=schemas.PartReportOut)
async def api_part(mpn: str, force: bool = False, _: None = Depends(api_rate_limit)):
    return await get_part_report(mpn, None, force=force)


@app.get("/api/sources")
def api_sources(_: None = Depends(api_rate_limit)):
    return source_status()


@app.get("/parts/{mpn}/export.{fmt}")
async def export_part(mpn: str, fmt: str):
    if fmt not in ("html", "json", "csv"):
        raise HTTPException(status_code=400, detail="format must be html, json or csv")
    report = await get_part_report(mpn)
    out_dir = _DATA_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "html":
        body = reports.part_html(report)
        path = out_dir / f"{mpn}.html"
        path.write_text(body, encoding="utf-8")
    elif fmt == "json":
        import json as _json

        path = out_dir / f"{mpn}.json"
        path.write_text(_json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    else:
        path = out_dir / f"{mpn}.csv"
        path.write_text(csv_part(report), encoding="utf-8-sig")
    return FileResponse(path, filename=path.name, media_type=_media(fmt))


def csv_part(report: schemas.PartReportOut) -> str:
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    w = _csv.writer(buf)
    c = report.component
    w.writerow(["field", "value"])
    for label, value in (
        ("mpn", c.mpn_normalized),
        ("manufacturer", c.manufacturer),
        ("category", c.category),
        ("package", c.package),
        ("lifecycle_status", c.lifecycle_status),
        ("family", c.family),
        ("description", c.description),
        ("rohs", c.rohs),
        ("reach", c.reach),
        ("operating_voltage", c.operating_voltage),
        ("current", c.current),
        ("frequency", c.frequency),
        ("temperature", c.temperature),
        ("risk_score", report.risk.score),
        ("risk_level", report.risk.level),
        ("confidence", report.confidence),
    ):
        w.writerow([label, value])
    w.writerow([])
    w.writerow(["distributor", "available_qty", "price_breaks", "lead_time", "currency", "checked_at"])
    for s in c.snapshots:
        pb = "; ".join(f"{b.qty}@{b.price}" for b in s.price_breaks)
        w.writerow([s.distributor, s.available_qty, pb, s.lead_time, s.currency, s.checked_at])
    return buf.getvalue()


def _media(fmt: str) -> str:
    return {
        "html": "text/html; charset=utf-8",
        "json": "application/json",
        "csv": "text/csv; charset=utf-8",
    }[fmt]


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    run()
