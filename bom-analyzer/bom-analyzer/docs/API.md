# REST API

`bomiq serve` (or the desktop app) exposes the engine over loopback JSON.

## Authentication and safety

Every route except `/api/ping` requires the session token printed at start-up,
supplied as the `X-BOMIQ-Token` header, an `Authorization: Bearer …` header,
or a `?t=` query parameter. In addition the server:

- binds `127.0.0.1` and refuses non-loopback client addresses
- validates the `Host` header, to block DNS rebinding
- rejects any request carrying a cross-origin `Origin`
- caps request bodies at 220 MB (8 MB for JSON)
- refuses static paths that escape the UI directory

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ping` | liveness (no token) |
| GET | `/api/status` | version, paths, provider stats, cache state, jobs |
| GET | `/api/bootstrap` | everything the UI needs on first paint |
| GET/POST | `/api/settings` | read / partially update settings |
| GET | `/api/providers` | provider catalogue and credential state (masked) |
| POST | `/api/providers/{id}/credentials` | store a key (`{"credentials":{…},"enable":true}`) |
| DELETE | `/api/providers/{id}/credentials` | remove a key |
| POST | `/api/providers/test` | live credential check |
| POST | `/api/upload` | upload a BOM (multipart or raw body) |
| GET | `/api/upload/{id}/preview` | header/preview for the mapping editor |
| POST | `/api/remap` | re-ingest with a corrected mapping / sheet / header row |
| POST | `/api/analyse` | start an analysis → `202` with a job |
| GET | `/api/jobs` | recent jobs |
| GET | `/api/jobs/{id}` | progress, stage, event log |
| GET | `/api/jobs/{id}/result` | the finished analysis |
| POST | `/api/jobs/{id}/cancel` | cancel a running analysis |
| GET | `/api/analysis/{id}` | an analysis already in memory |
| GET | `/api/analysis/{id}/export?format=…` | download a report |
| GET | `/api/analysis/{id}/price-comparison` | live prices vs the prices in the file |
| GET/POST | `/api/projects` | list / save |
| GET/DELETE | `/api/projects/{id}` | reopen / delete |
| GET | `/api/part?mpn=…&manufacturer=…` | single part lookup |
| POST | `/api/search` | keyword / parametric search |
| POST | `/api/alternates` | on-demand alternate search for one part |
| GET | `/api/fields` `/api/rules` `/api/templates` | reference data |
| DELETE | `/api/templates/{id}` | forget a saved column layout |
| POST | `/api/cache/prune` `/api/cache/clear` | cache maintenance |
| GET | `/api/audit` | local audit trail |
| POST | `/api/shutdown` | stop the server |

## Analysing a BOM

```bash
TOKEN=…   # printed at start-up
BASE=http://127.0.0.1:8756

# 1. upload
curl -s -H "X-BOMIQ-Token: $TOKEN" -F "file=@BOM.xlsx" $BASE/api/upload

# 2. start (use the upload_id from step 1)
curl -s -H "X-BOMIQ-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"upload_id":"…","settings":{"build_quantity":500}}' \
     $BASE/api/analyse

# 3. poll
curl -s -H "X-BOMIQ-Token: $TOKEN" $BASE/api/jobs/JOB_ID

# 4. collect
curl -s -H "X-BOMIQ-Token: $TOKEN" $BASE/api/jobs/JOB_ID/result

# 5. export
curl -s -H "X-BOMIQ-Token: $TOKEN" -o report.xlsx \
     "$BASE/api/analysis/ANALYSIS_ID/export?format=xlsx"
```

## Errors

Failures return JSON with a message written for a person:

```json
{"error": "Could not identify the required column(s): quantity. Detected headers: …",
 "status": 400}
```

| Status | Meaning |
|---|---|
| 400 | bad request — the message says what to fix |
| 401 | missing or wrong session token |
| 403 | cross-origin, unexpected `Host`, or a non-loopback client |
| 404 | unknown route, expired upload, or no such job/project |
| 405 | wrong method; `detail.allow` lists the right ones |
| 409 | the job is still running |
| 413 | body too large |
| 500 | unexpected — the local log has the traceback |

## The analysis payload

`/api/jobs/{id}/result` returns `{"analysis_id": "…", "analysis": {…}}`. The
analysis object is the serialised domain model:

```
bom        { name, source_file, lines[], column_map, metadata, issues[] }
results[]  { line, match, part, risk, cost, compliance, alternates[],
             completions[], issues[], provider_errors }
summary    { total_lines, matched_lines, total_cost, currency,
             risk_counts, lifecycle_counts, provider_stats, rules, … }
health     { score, grade, level, components, weights, headline, drivers[] }
issues[]   BOM-level findings
warnings[] things you should know about this run
offline    true when only the built-in catalogue was used
```

It round-trips: `POST /api/projects` stores exactly this, and
`GET /api/projects/{id}` gives it back. The Python model can reload it too:

```python
from bomiq.core.models import BomAnalysis
analysis = BomAnalysis.from_dict(json.load(open("report.json")))
```

## Using the engine directly

The API is a thin shell over the engine, which is easier to use from Python:

```python
from bomiq.config import Config
from bomiq.engine import Engine

config = Config()
config.settings.build_quantity = 500
engine = Engine(config)
analysis = engine.analyse_file("BOM.xlsx", progress=lambda e: print(e.message))

print(analysis.health.score, analysis.summary.total_cost)
for result in analysis.results:
    if result.risk.level.value in ("High", "Critical"):
        print(result.line.mpn, result.risk.score, result.risk.flags)

engine.close()
```
