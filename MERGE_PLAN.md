# Merge Plan: BOM-IQ Engine into Schemata

## Overview

Merge the BOM-IQ engine (7 providers, full ingest pipeline, 8-tab SPA UI, scoring
systems) into Schemata's FastAPI architecture. The result is a unified Component
Intelligence Platform with BOM-IQ's powerful BOM analysis as its centerpiece.

**Source**: `bom-analyzer/bom-analyzer/bomiq/` (BOM-IQ engine)
**Target**: `/` repo root (Schemata)

---

## Architecture Decision

```
BEFORE:
  Schemata (FastAPI) ── app/sources/ (3 adapters: Mouser, DigiKey, Nexar)
                     ── app/bom.py (simple sequential BOM analysis)
                     ── ui/templates/bom.html (server-rendered, 90 lines)

AFTER:
  Schemata (FastAPI) ── app/engine/bomiq/ (BOM-IQ package, adapted)
                     │    ├── providers/ (7 providers + mock)
                     │    ├── ingest/ (7-stage pipeline)
                     │    ├── analysis/ (match, risk, cost, alternates, compliance, health)
                     │    ├── export/ (xlsx, csv, json, html)
                     │    ├── core/ (models, db, errors)
                     │    └── util/ (http, text, units, money)
                     ├── app/engine/bridge.py (FastAPI ↔ Engine adapter)
                     ├── app/routes/bom_api.py (FastAPI router, 32 endpoints)
                     └── ui/bom-iq/ (SPA: index.html, app.js, styles.css)
```

---

## Phase 1: Copy BOM-IQ Package into Schemata

### 1.1 Create `app/engine/` directory structure

```
app/engine/
├── __init__.py
├── bomiq/                   # <-- copied from bom-analyzer/bom-analyzer/bomiq/
│   ├── __init__.py
│   ├── engine.py
│   ├── cli.py               # remove (Schemata has its own CLI)
│   ├── desktop.py           # remove (Schemata has its own launcher)
│   ├── config.py
│   ├── core/
│   ├── ingest/
│   ├── providers/
│   ├── analysis/
│   ├── export/
│   ├── server/              # keep ui/ subfolder only, remove http.py/app.py
│   │   └── ui/
│   │       ├── index.html
│   │       ├── app.js
│   │       └── styles.css
│   └── util/
├── bridge.py                # NEW: FastAPI ↔ Engine adapter
└── routes.py                # NEW: FastAPI router wrapping bomiq APIs
```

### 1.2 Files to COPY from BOM-IQ (with modifications)

| Source Path | Target Path | Notes |
|---|---|---|
| `bomiq/engine.py` | `app/engine/bomiq/engine.py` | Keep as-is initially |
| `bomiq/config.py` | `app/engine/bomiq/config.py` | Adapt paths for Schemata's data dir |
| `bomiq/core/*` | `app/engine/bomiq/core/` | Copy all |
| `bomiq/ingest/*` | `app/engine/bomiq/ingest/` | Copy all |
| `bomiq/providers/*` | `app/engine/bomiq/providers/` | Copy all, remove L&T branding |
| `bomiq/analysis/*` | `app/engine/bomiq/analysis/` | Copy all |
| `bomiq/export/*` | `app/engine/bomiq/export/` | Copy all |
| `bomiq/util/*` | `app/engine/bomiq/util/` | Copy all |
| `bomiq/server/ui/*` | `ui/bom-iq/` | SPA files (index.html, app.js, styles.css) |

### 1.3 Files to SKIP (not needed in Schemata)

| File | Reason |
|---|---|
| `bomiq/cli.py` | Schemata has its own entry point |
| `bomiq/desktop.py` | Schemata uses Tkinter launcher + Nuitka |
| `bomiq/server/app.py` | Replaced by FastAPI routes |
| `bomiq/server/http.py` | Replaced by FastAPI |
| `bomiq/server/jobs.py` | Replaced by FastAPI background tasks |

### 1.4 Branding cleanup

In ALL copied files, remove:
- "L&T Technology Services" / "LTTS" references
- "BOM-IQ" branding → replace with "Schemata"
- `com.ltts.bomiq` app ID → `com.schemata.bomiq`
- Update `__version__` and `__app_name__` in `__init__.py`

---

## Phase 2: FastAPI Bridge Layer

### 2.1 `app/engine/bridge.py` — Engine Lifecycle Manager

```python
"""FastAPI ↔ BOM-IQ Engine bridge."""

from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from app.engine.bomiq.engine import Engine
from app.engine.bomiq.config import Config

_executor = ThreadPoolExecutor(max_workers=4)
_engine: Engine | None = None

def get_engine() -> Engine:
    """Get or create the singleton Engine."""
    global _engine
    if _engine is None:
        _engine = Engine(config=Config())
    return _engine

async def run_engine_method(method_name: str, *args, **kwargs) -> Any:
    """Run a blocking Engine method in a thread pool."""
    engine = get_engine()
    method = getattr(engine, method_name)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: method(*args, **kwargs))
```

### 2.2 `app/engine/routes.py` — FastAPI Router (32 endpoints)

Convert BOM-IQ's stdlib routes to FastAPI `APIRouter`:

```python
from fastapi import APIRouter, UploadFile, BackgroundTasks
router = APIRouter(prefix="/api/bom", tags=["bom"])

# Key route mappings (bomiq stdlib → FastAPI):
POST /api/bom/upload        ← /api/upload
POST /api/bom/remap         ← /api/remap
POST /api/bom/analyse       ← /api/analyse (returns 202 + job_id)
GET  /api/bom/jobs/{id}     ← /api/jobs/{job_id}
GET  /api/bom/jobs/{id}/result ← /api/jobs/{job_id}/result
GET  /api/bom/export/{id}   ← /api/analysis/{analysis_id}/export
GET  /api/bom/settings      ← /api/settings
POST /api/bom/settings      ← /api/settings
GET  /api/bom/providers     ← /api/providers
POST /api/bom/providers/{id}/credentials ← /api/providers/{id}/credentials
POST /api/bom/search        ← /api/search
POST /api/bom/alternates    ← /api/alternates
GET  /api/bom/projects      ← /api/projects
POST /api/bom/projects      ← /api/projects
# ... etc (all 32 routes)
```

Each handler:
1. Extracts request data
2. Calls `await run_engine_method(...)` 
3. Returns JSON response

### 2.3 Integration with existing Schemata routes

In `app/main.py`, mount the new router:

```python
from app.engine.routes import router as bom_router
app.include_router(bom_router)
```

Update navigation in `base.html`:
- "BOM desk" → links to `/bom-iq` (the SPA)
- Keep "Part library" and "Sources" as-is

---

## Phase 3: Replace Source Adapters

### 3.1 Remove Schemata's old adapters

| File | Action |
|---|---|
| `app/sources/base.py` | Keep for backward compat, but BOM-IQ's Provider is now primary |
| `app/sources/mouser.py` | Remove (BOM-IQ has its own MouserProvider) |
| `app/sources/digikey.py` | Remove (BOM-IQ has its own DigiKeyProvider) |
| `app/sources/nexar.py` | Remove (BOM-IQ has its own NexarProvider) |
| `app/sources/local.py` | Remove (BOM-IQ has MockProvider + offline catalog) |
| `app/sources/demo_catalog.py` | Remove (BOM-IQ has built-in catalog with ~55 real parts) |
| `app/orchestrator.py` | Remove (BOM-IQ's ProviderRegistry replaces this) |

### 3.2 Update `app/service.py`

The existing `get_part_report()` function uses the old orchestrator. Update to:
1. Use `Engine.registry()` from BOM-IQ for part lookups
2. Keep the SQLAlchemy persistence layer (it works well)
3. Bridge between BOM-IQ's `PartData` and Schemata's `Component` model

### 3.3 Update `app/config.py`

- Merge BOM-IQ's env var handling (BOMIQ_*) with Schemata's existing config
- Both already read `.env` and `config.toml` — align the patterns
- Credentials: BOM-IQ uses OS keyring; Schemata uses `.env`. Support both.

### 3.4 Update `pyproject.toml` dependencies

```toml
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "sqlalchemy>=2.0.25",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "httpx>=0.27",
    "openpyxl>=3.1",
    # NEW: optional BOM-IQ deps (all have stdlib fallbacks)
    # "rapidfuzz",     # optional: better fuzzy matching
    # "xlrd",          # optional: .xls support
    # "keyring",       # optional: OS credential store
]
```

No new required dependencies — BOM-IQ is zero-dep by design.

---

## Phase 4: Frontend Integration

### 4.1 Mount BOM-IQ SPA

Add to `app/main.py`:

```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Mount BOM-IQ SPA static assets
app.mount("/bom-iq/static", StaticFiles(directory="ui/bom-iq"), name="bom-iq-static")

@app.get("/bom-iq")
@app.get("/bom-iq/{path:path}")
async def serve_bom_iq(path: str = ""):
    """Serve the BOM-IQ SPA."""
    if path.startswith("api/"):
        pass  # handled by FastAPI router
    return FileResponse("ui/bom-iq/index.html")
```

### 4.2 SPA API path adaptation

BOM-IQ's SPA calls `/api/*` endpoints. Two options:

**Option A (Recommended):** Add a reverse proxy in FastAPI that routes `/api/bom/*` requests to the BOM-IQ router. The SPA's existing `/api/*` calls work unchanged.

**Option B:** Modify the SPA's `app.js` to prefix all API calls with `/api/bom/`. This is a simple find-replace in the JS file.

Go with **Option A** — mount the BOM-IQ routes at `/api/` prefix (not `/api/bom/`) so the SPA works without modification. The existing Schemata `/api/*` routes (parts, settings, sources) live alongside.

### 4.3 Remove old BOM UI

| File | Action |
|---|---|
| `ui/templates/bom.html` | Remove (replaced by SPA) |
| `app/bom.py` | Remove (replaced by BOM-IQ engine) |
| `ui/static/js/app.js` | Remove `bomDrop()` function (no longer needed) |

### 4.4 Update navigation

In `ui/templates/base.html`:
```html
<nav class="nav">
  <a href="/parts">Part library</a>
  <a href="/bom-iq">BOM Analyzer</a>    <!-- was /bom -->
  <a href="/settings">Sources</a>
</nav>
```

### 4.5 Brand the SPA

In `ui/bom-iq/index.html`:
- Change title: "BOM-IQ" → "Schemata — BOM Analyzer"
- Change brand mark: "IQ" → Schemata logo/brand
- Update any "BOM-IQ" text references to "Schemata"

---

## Phase 5: Provider & Credential Integration

### 5.1 Unified credential management

Schemata currently stores API keys in `.env`. BOM-IQ uses OS keyring with `.env` fallback.

**Strategy:** Keep `.env` as primary (users are familiar with it), but also support keyring.

Update `app/engine/bomiq/config.py`:
1. Read credentials from `.env` first (Schemata pattern)
2. Fall back to keyring if available
3. Support all 7 providers' env vars:
   - `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`
   - `MOUSER_API_KEY`
   - `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET`
   - `ARROW_API_KEY`
   - `FARNELL_API_KEY`
   - `LCSC_API_KEY`
   - `TRUSTEDPARTS_API_KEY`

### 5.2 Update settings page

The existing `/settings` page needs to handle credentials for all 7 providers (currently only 3). Either:
- Extend the existing `settings.html` template to show all 7
- Or let the BOM-IQ SPA's Settings modal handle it (it already has a full settings UI)

**Recommendation:** Use the BOM-IQ SPA's Settings modal for BOM-related config (providers, analysis params, risk weights). Keep Schemata's `/settings` page for app-level config (display preferences, export paths).

---

## Phase 6: Update Build Pipeline

### 6.1 Nuitka build script updates

In `packaging/build_nuitka.ps1`:

```powershell
# ADD: Include the BOM-IQ engine package
"--include-package=app.engine"

# ADD: Include BOM-IQ SPA assets
"--include-data-dir=$root\ui\bom-iq=bom-iq"

# ADD: Include bomiq data files (sample BOMs, etc.)
"--include-data-dir=$root\app\engine\bomiq\samples=bomiq_samples"

# UPDATE: Product version
--windows-product-version=2.0.0
--product-version=2.0.0

# UPDATE: File description
"--windows-file-description=Schemata - Component Intelligence Platform with BOM Analyzer"
```

### 6.2 Inno Setup updates

In `packaging/Schemata-setup.iss`:

```
#define AppVersion    "2.0.0"
```

### 6.3 Version bump

Update `pyproject.toml`:
```toml
version = "2.0.0"
description = "Component Intelligence Platform — lifecycle, availability, pricing, BOM risk intelligence with 7-source enrichment"
```

---

## Phase 7: Testing & Verification

### 7.1 Test strategy

| Area | Action |
|---|---|
| BOM-IQ engine tests | Copy `bom-analyzer/bom-analyzer/tests/` → `tests/engine/`, adapt imports |
| Existing Schemata tests | Run `pytest` — ensure no regressions |
| Integration test | Upload a sample BOM through the SPA, verify full pipeline |
| Provider test | Run `bomiq providers --test` equivalent via FastAPI |
| Build test | Run Nuitka build, verify `Schemata.exe` launches |

### 7.2 Verification checklist

- [ ] BOM-IQ SPA loads at `/bom-iq`
- [ ] All 7 providers configurable via Settings
- [ ] BOM upload → column mapping → analysis → results tabs work
- [ ] Part search works from SPA
- [ ] Existing `/parts` library still works
- [ ] Existing `/settings` still works
- [ ] Nuitka build succeeds
- [ ] No L&T Technology Services references remain
- [ ] All tests pass

---

## Execution Order

| Step | Phase | Effort | Dependencies |
|---|---|---|---|
| 1 | Phase 1: Copy BOM-IQ package | 1-2h | None |
| 2 | Phase 2: FastAPI bridge + routes | 3-4h | Step 1 |
| 3 | Phase 3: Replace source adapters | 2-3h | Steps 1-2 |
| 4 | Phase 4: Frontend integration | 2-3h | Step 2 |
| 5 | Phase 5: Provider/credential merge | 1-2h | Steps 2-3 |
| 6 | Phase 6: Build pipeline update | 1h | Steps 1-5 |
| 7 | Phase 7: Testing & verification | 2-3h | Steps 1-6 |
| **Total** | | **12-18h** | |

---

## Risk Mitigation

1. **Import conflicts**: BOM-IQ's `config.py` may clash with Schemata's `app/config.py`. Solution: the engine lives in `app.engine.bomiq.config` — no collision.

2. **SQLite schema differences**: BOM-IQ uses `bomiq.sqlite3`, Schemata uses `part_intel.db`. Solution: BOM-IQ creates its own DB file in the data directory — no schema conflict.

3. **Circular imports**: Bridge layer must not import from both `app.service` and `app.engine.bomiq` in the same module. Solution: keep bridge imports clean and one-directional.

4. **SPA CORS**: The SPA uses root-relative paths (`/api/*`). If mounted at `/bom-iq/`, the paths won't resolve. Solution: either proxy or keep API routes at root level.

5. **Build size**: Adding BOM-IQ's7 providers and analysis modules will increase the Nuitka bundle. Monitor and optimize with `--nofollow-import-to` as needed.

---

## Post-Merge: Future Web Deployment

The user plans to deploy to free web providers. After the merge:

1. The FastAPI app is already ASGI-compatible (uvicorn)
2. Deploy to Railway, Render, or Fly.io free tier
3. BOM-IQ's stdlib HTTP client → ensure `httpx` is used for provider calls in production
4. SQLite → consider PostgreSQL for production (SQLAlchemy makes this easy)
5. SPA can be served as static files from any CDN

This architecture supports both desktop (Nuitka) and web (cloud) deployment.
