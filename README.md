# Schemata — Component Intelligence Platform

A **local, privacy-first component intelligence platform** for electronics engineers and procurement. `Schemata` tracks a component's full lifecycle: search a part number, see its lifecycle status, distributor stock & pricing, engineering risk, and replacement candidates — then roll a whole **BOM** through a line-by-line procurement risk pipeline.

> Runs 100% locally. The server binds to `127.0.0.1` only. Nothing leaves your machine except direct look-ups to Mouser / DigiKey when you configure API keys. No telemetry, no accounts, no cloud.

---

## ✨ Features

- **Part search & dossier**
  - Lifecycle status (active → obsolete) with colour-coded timeline
  - Live distributor snapshots: stock, price breaks, MOQ, lead time, ROHS/REACH compliance
  - Price & stock **history charts** (vanilla canvas, no chart lib)
  - **Engineering risk score** — weighted lifecycle, stock, lead-time, vendor diversity, price trend & manufacturer
  - Replacement / alternative candidate suggestions
  - Export per-part dossier as **HTML / JSON / CSV**

- **BOM desk**
  - Upload a BOM (`.csv`, `.txt`, `.xlsx`, `.xlsm`) — one column enumerates the lines
  - Per-line procurement risk, criticals, cost roll-up and lifecycle distribution
  - Export the report as **JSON / CSV / HTML**

- **Sources**
  - Live adapters: **Mouser Search API** and **DigiKey Product Information + OAuth2**
  - Built-in **demo catalog** so a fresh install is useful with zero keys
  - "Test connection" probe against live sources from the settings page

- **Operations**
  - Bounded in-memory BOM cache (TTL + max entries), persistent per-user storage
  - Inbound API rate limiting + per-source outbound throttle (respects Mouser 30/min hard limit)
  - Structured logging (`%LOCALAPPDATA%\Schemata\launcher.log`)
  - Native **desktop launcher** (Tk) to start/stop the server and open the browser
  - Portable `Schemata.exe` and a Windows installer

---

## 🖥️ Installation

### Option A — Windows binary (recommended)

Use the installer from the **Releases** page, or unzip the portable build:

1. Run `Setup_Schemata_v1.0.0.exe` and follow the wizard (installs the VC++ runtime automatically),
   **or** unzip `Schemata.dist.zip` and run `Schemata.exe` from anywhere.
2. Click **Start Schemata**. Your browser opens at `http://127.0.0.1:8750`.
3. First run seeds the demo catalog automatically.

All user data (database, cached reports, exports and `.env` credentials) lives in
`%LOCALAPPDATA%\Schemata\` — it survives uninstall and needs no admin rights to write.

### Option B — From source

Requires **Python 3.11+**.

```powershell
git clone https://github.com/KSHNKMRJHA/schemata.git
cd schemata
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"

# run it
python -m app.main            # or: part-search
# open http://127.0.0.1:8000
```

---

## ⚙️ Configuration

Secrets go in `.env` (copy from `.env.example`); behaviour knobs go in `config.toml`.

```
MOUSER_API_KEY=            # https://www.mouser.com/api-hub/
DIGIKEY_CLIENT_ID=         # https://developer.digikey.com
DIGIKEY_CLIENT_SECRET=
```

> Missing keys fall back to the seeded demo catalog automatically — the app is fully
> usable offline. Credentials are stored per-user and are never sent to the repository.

`config.toml` highlights:

| Section | Keys | Purpose |
| --- | --- | --- |
| `[defaults]` | `region`, `currency`, `site`, `language` | Localisation defaults |
| `[cache]` | `component_ttl_hours`, `snapshot_ttl_minutes`, `lifecycle_min_interval_hours` | Fact freshness windows |
| `[orchestrator]` | `timeout_seconds`, `max_retries`, `concurrency` | Live-source fan-out |
| `[rate_limits]` | `mouser_per_minute`, `digikey_per_minute` | Outbound throttle per source |
| `[api]` | `requests_per_minute`, `probe_mpn`, `probe_manufacturer` | Inbound rate limit & probe part |
| `[risk]` | `weight_*` | Risk-score weighting |

---

## 🛠️ Development

```powershell
.\.venv\Scripts\python -m pytest          # 46 tests
.\.venv\Scripts\python -m ruff check app ui tests
```

## 📦 Building the binary (Windows)

```powershell
# 1) Standalone exe  -> dist\Schemata.dist\Schemata.exe
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_nuitka.ps1

# 2) Inno Setup installer -> dist\installer\Setup_Schemata_v1.0.0.exe
& "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\Schemata-setup.iss
```

Requires `Nuitka` (in `[dev]` extras), a MinGW64 toolchain, and **Inno Setup 6**.

---

## 🔌 API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/parts?limit=` | Recently seen components (1–500) |
| `GET` | `/api/parts/{mpn}` | Full part dossier (JSON) |
| `GET` | `/api/sources` | Configured live/mock source status |
| `POST` | `/api/settings` | Persist credential overrides |
| `POST` | `/api/settings/test` | Probe configured live sources |
| `POST` | `/bom/analyze` | Upload & analyze a BOM (multipart) |
| `GET` | `/bom/export/{token}.{html,json,csv}` | Export a BOM report |

All `/api/*` endpoints are rate-limited (`[api] requests_per_minute`, default 600/min).

---

## 🗂️ Project layout

```
app/            Backend: config, models, sources (mouser/digikey/mock),
                orchestrator, service, BOM engine, reports, FastAPI app
ui/             Jinja2 templates, CSS and vanilla JS
packaging/      launcher.py (Tk start window), Nuitka build, Inno Setup .iss
tests/          pytest suite (unit + API integration)
config.toml     Runtime tunables
.env.example    Credentials template
```

---

## 🙌 Credits

Built and maintained by **Kishan J.**
- GitHub: [KSHNKMRJHA](https://github.com/KSHNKMRJHA)

Powered by **Mouser API Hub** and **DigiKey Product Information API** data where configured.

---

## 📬 Contact

- **Issues & feature requests:** open a [GitHub issue](../../issues)
- **Questions / feedback:** [KSHNKMRJHA](https://github.com/KSHNKMRJHA)

---

## 📄 License

[MIT](LICENSE) © 2026 Kishan J.