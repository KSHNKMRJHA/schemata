"""FastAPI application entrypoint + UI routes."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from app import reports, schemas
from app.bom import analyze_rows, export_bom_csv, export_bom_json, parse_bom_file, to_bom_report_out
from app.config import (
    DATA_DIR,
    ENV_PATH,
    FROZEN,
    UI_DIR,
    config_section,
    currency,
    get_settings,
    region,
    reload_settings,
    write_env,
)
from app.db import init_db
from app.history import build_history
from app.lifecycle import (
    allowed_transitions,
    status_color,
    status_description,
)
from app.orchestrator import get_orchestrator, reload_orchestrator
from app.ratelimit import api_rate_limit
from app.service import get_part_report, recent_parts
from app.sources.digikey import reset_token_cache as reset_digikey_token_cache
from app.sources.nexar import reset_token_cache as reset_nexar_token_cache

logger = logging.getLogger(__name__)

_DATA_DIR = DATA_DIR
_EXPORT_DIR = _DATA_DIR / "bom"
_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

init_db()

@asynccontextmanager
async def _lifespan(_: FastAPI):
    """Validate required directories and database availability on startup."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - a broken DB must fail startup loudly
        logger.critical("Database initialization failed: %s", exc)
        raise
    yield
    _bom_store.clear()


app = FastAPI(title="Part Intelligence", version="0.1.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")

_env = Environment(
    loader=FileSystemLoader(str(UI_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
_env.filters["status_color"] = status_color
_env.filters["status_desc"] = status_description
_env.filters["datetime"] = lambda dt: dt.strftime("%Y-%m-%d") if dt else "—"

_bom_store: OrderedDict[str, tuple[float, schemas.BomReportOut]] = OrderedDict()
# Bound in-memory growth: keep at most N reports, each for at most 24 hours.
_BOM_MAX_ENTRIES = 1000
_BOM_TTL_SECONDS = 24 * 60 * 60


def _prune_bom_store(now: float) -> None:
    while _bom_store:
        created, _ = next(iter(_bom_store.values()))
        if now - created <= _BOM_TTL_SECONDS and len(_bom_store) <= _BOM_MAX_ENTRIES:
            break
        _bom_store.popitem(last=False)


def _cache_bom(token: str, report: schemas.BomReportOut, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    _bom_store[token] = (now, report)
    _bom_store.move_to_end(token)
    _prune_bom_store(now)


def _render(template: str, ctx: dict | None = None) -> HTMLResponse:
    ctx = dict(ctx or {})
    ctx.setdefault("live_sources", get_orchestrator().live_sources())
    ctx.setdefault("mock_sources", get_orchestrator().mock_sources())
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
    orch = get_orchestrator()
    return {
        "live": orch.live_sources(),
        "mock": orch.mock_sources(),
        "configured": orch.configured(),
    }


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


@app.get("/bom", response_class=HTMLResponse)
def bom_page():
    return _render("bom.html", {})


@app.post("/bom/analyze")
async def bom_analyze(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".csv", ".txt", ".xlsx", ".xlsm"):
        raise HTTPException(status_code=400, detail="Upload a .csv, .txt, .xlsx or .xlsm file.")
    tmp = _DATA_DIR / "bom_upload" / f"{uuid.uuid4().hex}{suffix}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(await file.read())
    try:
        rows = parse_bom_file(tmp)
        report = await analyze_rows(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("BOM parsing failed for %s: %s", file.filename, exc)
        raise HTTPException(status_code=400, detail=f"BOM parsing failed: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    out = to_bom_report_out(report, file.filename)
    token = uuid.uuid4().hex
    path = _EXPORT_DIR / f"{token}.json"
    path.write_text(out.model_dump_json(indent=2), encoding="utf-8")
    _cache_bom(token, out)
    return RedirectResponse(f"/bom/result/{token}", status_code=303)


@app.get("/bom/result/{token}", response_class=HTMLResponse)
def bom_result(token: str):
    out = _load_bom(token)
    return _render("bom.html", {"result": out, "token": token})


@app.get("/bom/export/{token}.{fmt}")
def bom_export(token: str, fmt: str):
    if fmt not in ("html", "json", "csv"):
        raise HTTPException(status_code=400, detail="format must be html, json or csv")
    out = _load_bom(token)
    path = _EXPORT_DIR / f"{token}.{fmt}"
    if fmt == "html":
        path.write_text(reports.bom_html(out), encoding="utf-8")
    elif fmt == "json":
        path.write_text(export_bom_json(out), encoding="utf-8")
    else:
        path.write_text(export_bom_csv(out), encoding="utf-8-sig")
    return FileResponse(path, filename=f"bom_report.{fmt}", media_type=_media(fmt))


def _load_bom(token: str) -> schemas.BomReportOut:
    now = time.monotonic()
    entry = _bom_store.get(token)
    if entry is not None:
        if now - entry[0] > _BOM_TTL_SECONDS:
            _bom_store.pop(token, None)
        else:
            _bom_store.move_to_end(token)
            _prune_bom_store(now)
            return entry[1]
    path = _EXPORT_DIR / f"{token}.json"
    if path.exists():
        out = schemas.BomReportOut.model_validate_json(path.read_text(encoding="utf-8"))
        _cache_bom(token, out, now)
        return out
    logger.warning("BOM report not found for token=%s", token)
    raise HTTPException(status_code=404, detail="BOM report not found — re-run the analysis.")


class SettingsPayload(BaseModel):
    mouser_api_key: str = ""
    digikey_client_id: str = ""
    digikey_client_secret: str = ""
    digikey_sandbox: bool = False
    nexar_client_id: str = ""
    nexar_client_secret: str = ""


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    orch = get_orchestrator()
    s = get_settings()
    return _render(
        "settings.html",
        {
            "configured": orch.configured(),
            "current": {
                "mouser_api_key": _masked(s.mouser_api_key),
                "digikey_client_id": _masked(s.digikey_client_id),
                "digikey_client_secret": _masked(s.digikey_client_secret),
                "digikey_sandbox": s.digikey_sandbox,
                "nexar_client_id": _masked(s.nexar_client_id),
                "nexar_client_secret": _masked(s.nexar_client_secret),
            },
            "demo_enabled": True,
            "env_path": str(ENV_PATH),
            "frozen": FROZEN,
        },
    )


def _masked(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * 8 + value[-keep:]


@app.post("/api/settings")
def api_settings_save(payload: SettingsPayload, _: None = Depends(api_rate_limit)):
    try:
        written = write_env(payload.model_dump())
    except ValueError as exc:
        logger.warning("Settings save rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    reload_settings()
    reset_digikey_token_cache()
    reset_nexar_token_cache()
    orch = reload_orchestrator()
    return {
        "saved": written,
        "configured": orch.configured(),
        "live": orch.live_sources(),
        "mock": orch.mock_sources(),
    }


@app.post("/api/settings/test")
async def api_settings_test(_: None = Depends(api_rate_limit)):
    """Probe each configured live adapter with a known-good part."""
    from app.models import SourceType

    orch = get_orchestrator()
    api_cfg = config_section("api")
    known = (
        api_cfg.get("probe_mpn") or "NE555P",
        api_cfg.get("probe_manufacturer") or "Texas Instruments",
    )
    results = {}
    for adapter in orch.adapters:
        if adapter.source_type == SourceType.MOCK:
            results[adapter.name] = {"configured": False, "note": "demo data — no live probe"}
            continue
        t0 = time.monotonic()
        try:
            res = await adapter.search(known[0], known[1], region(), currency())
            elapsed = round((time.monotonic() - t0) * 1000, 1)
            ok = bool(res.facts or res.offers) or not res.errors
            results[adapter.name] = {
                "configured": True,
                "ok": ok,
                "ms": elapsed,
                "error": (res.errors or [None])[0],
                "url": res.source_url,
            }
        except Exception as exc:  # noqa: BLE001 - surface network failures per source
            elapsed = round((time.monotonic() - t0) * 1000, 1)
            logger.warning("Probe failed for %s: %s", adapter.name, exc)
            results[adapter.name] = {"configured": True, "ok": False, "ms": elapsed, "error": str(exc)}
    return {"probe": known[0], "sources": results, "frozen": FROZEN}


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
