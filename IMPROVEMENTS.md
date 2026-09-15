# Part Intelligence - Improvement Opportunities

## 🔴 High Priority Issues

### 1. **Memory Leak in BOM Storage** (Line 48 in main.py)
**Issue**: `_bom_store` is an unbounded in-memory dictionary that grows indefinitely.
```python
_bom_store: dict[str, schemas.BomReportOut] = {}  # Never cleaned up!
```
**Impact**: Long-running server will consume unlimited memory as users upload BOMs.
**Recommendation**: 
- Add TTL-based cleanup (e.g., keep only last 1000 results or 24-hour-old)
- Use `functools.lru_cache` with maxsize instead
- Or implement a background cleanup task

---

### 2. **Code Duplication in Render Function** (Lines 52-68 in main.py)
**Issue**: The `_render()` function repeats the same dictionary construction twice.
```python
def _render(template: str, ctx: dict | None = None) -> HTMLResponse:
    return HTMLResponse(
        tmpl.render(
            **(
                {
                    "live_sources": get_orchestrator().live_sources(),
                    "mock_sources": get_orchestrator().mock_sources(),
                    **ctx,
                }
                if ctx
                else {
                    "live_sources": get_orchestrator().live_sources(),
                    "mock_sources": get_orchestrator().mock_sources(),
                }
            )
        )
    )
```
**Impact**: Unnecessary complexity, harder to maintain.
**Recommendation**: Simplify to:
```python
def _render(template: str, ctx: dict | None = None) -> HTMLResponse:
    base = {
        "live_sources": get_orchestrator().live_sources(),
        "mock_sources": get_orchestrator().mock_sources(),
    }
    if ctx:
        base.update(ctx)
    return HTMLResponse(_env.get_template(template).render(**base))
```

---

### 3. **No Input Validation/Limits on List Endpoints** (Lines 156, 180 in main.py)
**Issue**: Endpoints return fixed limits without validation.
```python
def parts_page():
    parts = recent_parts(40)  # Hard-coded, no user control

@app.get("/api/parts", response_model=list[schemas.ComponentOut])
def api_recent():
    return [to_component_out(c) for c in recent_parts(50)]  # Could be expensive
```
**Impact**: Potential DOS vulnerability, API inconsistency.
**Recommendation**: Add optional `limit` parameter with reasonable bounds:
```python
@app.get("/api/parts")
def api_recent(limit: int = Query(50, ge=1, le=500)):
    return [to_component_out(c) for c in recent_parts(limit)]
```

---

## 🟡 Medium Priority Issues

### 4. **Missing Structured Logging**
**Issue**: No logging framework; errors logged to console only.
```python
except Exception as exc:  # noqa: BLE001
    raise HTTPException(status_code=404, detail=str(exc)) from exc
```
**Impact**: 
- No audit trail or debugging capability
- Can't track user behavior or errors over time
- Frozen binary has no persistent logs

**Recommendation**: Add Python `logging` module:
```python
import logging
logger = logging.getLogger(__name__)
logger.error(f"Search failed for MPN={mpn}", exc_info=exc)
```

---

### 5. **JavaScript Using Callbacks Instead of Modern Async** (app.js)
**Issue**: Heavy callback nesting in fetch code, hard to read.
```javascript
fetch("/api/settings", {...})
  .then(function (r) { return r.json().then(function (d) { return { r: r, d: d }; }); })
  .then(function (o) { ... })
  .catch(function (err) { ... });
```
**Impact**: 
- Harder to maintain
- Less readable error handling
- Inconsistent with modern patterns

**Recommendation**: Modernize to async/await:
```javascript
async function saveSettings() {
  try {
    const r = await fetch("/api/settings", {...});
    const d = await r.json();
    if (r.status !== 200) throw new Error(d.detail || "save failed");
    // handle success
  } catch (err) {
    setStatus("Save failed: " + err.message, "err");
  }
}
```

---

### 6. **SQLite Connection Pool Not Configured** (db.py:14-21)
**Issue**: Default SQLite engine with no connection pooling tuning.
```python
_engine = create_engine(
    get_settings().database_url,
    connect_args={"check_same_thread": False},
)
```
**Impact**: May cause "database is locked" errors under concurrent load.
**Recommendation**: Add pool configuration:
```python
from sqlalchemy.pool import StaticPool
_engine = create_engine(
    get_settings().database_url,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # or NullPool for SQLite
)
```

---

### 7. **No Startup Validation**
**Issue**: No checks that required directories/configs exist on startup.
**Recommendation**: Add startup event:
```python
@app.on_event("startup")
async def startup():
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    # Verify database is accessible
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Database initialization failed: {e}")
        raise
```

---

## 🟢 Low Priority / Nice-to-Have

### 8. **Inconsistent Error Messages**
Some endpoints use `HTTPException(400, ...)`, others use `HTTPException(status_code=400, ...)`. Consider standardizing.

### 9. **No Rate Limiting**
Public API endpoints have no rate limiting. Consider adding `slowapi` or similar.

### 10. **Missing Type Hints**
Some callback functions in JavaScript lack JSDoc comments. Consider adding for future maintainability.

### 11. **Hardcoded Configuration Values**
- Known test part: `("NE555P", "Texas Instruments")` in line 347
- Cache TTL values in multiple places

**Recommendation**: Move to config.toml or environment variables.

---

## Summary Statistics

| Category | Count |
|----------|-------|
| Critical Issues | 3 |
| Important Issues | 4 |
| Nice-to-Have | 4 |
| **Total Actionable Items** | **11** |

