# Schemata — Component Intelligence Platform

A **local, privacy-first component intelligence platform** for electronics engineers and procurement. `Schemata` tracks a component's full lifecycle: search a part number, see its lifecycle status, distributor stock & pricing, engineering risk, and replacement candidates — then roll a whole **BOM** through a line-by-line procurement risk pipeline.

> **Try it live** — <https://schemata-359w.onrender.com> (free tier, see [Live Web](#-live-web-schemata-on-render) below).
>
> Runs 100% locally by default. The server binds to `127.0.0.1` only. Nothing leaves your machine except direct look-ups to Mouser / DigiKey / Nexar (Octopart) / Arrow / Farnell / LCSC / TrustedParts when you configure API keys. No telemetry, no accounts, no cloud.

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
  - Live adapters: **Mouser Search API**, **DigiKey Product Information + OAuth2**, **Nexar (Octopart) GraphQL**, **Arrow Electronics**, **Farnell / element14**, **LCSC Electronics** and **TrustedParts**
  - Built-in **demo catalog** so a fresh install is useful with zero keys
  - "Test connection" probe against live sources from the settings page
  - Per-source outbound throttle tuned to each provider's hard rate limits

- **Operations**
  - Bounded in-memory BOM cache (TTL + max entries), persistent per-user storage
  - Inbound API rate limiting + per-source outbound throttle (respects each provider's hard limit)
  - Structured logging (`%LOCALAPPDATA%\Schemata\launcher.log`)
  - Native **desktop launcher** (Tk) to start/stop the server and open the browser
  - Portable `Schemata.exe` and a Windows installer

---

## 🖥️ Installation

### Option A — Windows binary (recommended)

Use the installer from the **Releases** page, or unzip the portable build:

1. Run `Setup_Schemata_v2.3.0.exe` and follow the wizard (installs the VC++ runtime automatically),
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

### 🔑 Obtaining API keys (optional — needed for live data)

Live distributor data requires keys. Without them the app runs fully offline on the bundled demo catalog.

**Mouser Search API**
1. Register (free) at <https://www.mouser.com/api-hub/>.
2. Create an API key under *My Account → API*.
3. Result: one `MOUSER_API_KEY` token.

**DigiKey Product Information API**
1. Register at <https://developer.digikey.com>.
2. Create a new application → you get a **Client ID** and **Client Secret** (OAuth2 two-legged). No approval needed.
3. Result: `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`.

**Nexar (Octopart) API**
1. Register (free) at <https://nexar.com> → create an application.
2. Copy the **Client ID** and **Client Secret** (OAuth2 client_credentials). Free "Evaluation" plan includes 100 matched parts/month; no datasheets/lifecycle data on free tier.
3. Result: `NEXAR_CLIENT_ID` + `NEXAR_CLIENT_SECRET`.

**Arrow Electronics API**
1. Register at <https://developers.arrow.com> and request an **API Key**.
2. Result: `ARROW_API_KEY`. An optional `ARROW_LOGIN` (account e-mail) can be added for personalised quotes.
3. Arrow is available but **not enabled by default** — tick it under *Settings → Providers* after adding the key.

**Farnell / element14 API**
1. Register at <https://partner.element14.com> and create a **Product Search API** key.
2. Result: `FARNELL_API_KEY`. Optionally set `FARNELL_STORE` to your element14 store (e.g. `uk.farnell.com`, `in.element14.com`, `sg.element14.com`).
3. Farnell is available but **not enabled by default** — tick it under *Settings → Providers* after adding the key.

**LCSC Electronics API**
1. Create an account at <https://www.lcsc.com>, then apply for the open API under *Account → Open API*.
2. Result: `LCSC_API_KEY` + `LCSC_API_SECRET`.
3. Public endpoints work without a key (rate-limited and unofficial), so LCSC is fully usable even before adding keys.

**TrustedParts API**
1. Register at <https://www.trustedparts.com> (aggregation of **authorised** distributor stock — most useful for scarce/obsolete parts).
2. Result: `TRUSTEDPARTS_API_KEY`.

### 💡 How live sourcing works

- A provider is queried only when it is **selected** in *Settings → Providers* **and** has its required keys configured (or has no key requirement, like LCSC's public endpoints and the offline catalogue).
- On every analysis Schemata fans out to each enabled provider, merges the snapshots and applies a weighted risk score. If nothing is configured, it falls back to the bundled **demo catalog** automatically — the app is always usable.
- The Settings page shows live credential status (present / from `.env` / missing) and a **Test connection** probe for each configured source.

### 📍 Where to put the keys

| Run mode | Location | How |
| --- | --- | --- |
| **From source** | project root → `.env` | Copy `.env.example`, fill values, restart. |
| **Installed / portable binary** | `%LOCALAPPDATA%\Schemata\.env` | Open the app → **Settings** page → save, **or** edit the file directly. |

Inside the app, the Settings page always works and writes to `%LOCALAPPDATA%\Schemata\.env` per user —
it never touches the installation folder, so keys survive updates and uninstalls.

> 🔒 **Keys stay on your machine.** They are never bundled into the published executable, zip, or
> installer (the build pipeline asserts this), never written to the repository, and never sent anywhere
> except the distributor API you configured. The application also regenerates its own local secrets
> per user at install time.

### Secrets & tunables

Secrets go in `.env` (copy from `.env.example`); behaviour knobs go in `config.toml`.

```
MOUSER_API_KEY=            # https://www.mouser.com/api-hub/
DIGIKEY_CLIENT_ID=         # https://developer.digikey.com
DIGIKEY_CLIENT_SECRET=
NEXAR_CLIENT_ID=           # https://identity.nexar.com
NEXAR_CLIENT_SECRET=
ARROW_API_KEY=             # https://developers.arrow.com
# ARROW_LOGIN=             # optional account e-mail for personalised quotes
FARNELL_API_KEY=           # https://partner.element14.com
# FARNELL_STORE=           # e.g. uk.farnell.com
LCSC_API_KEY=              # https://www.lcsc.com -> Account -> Open API
LCSC_API_SECRET=
TRUSTEDPARTS_API_KEY=      # https://www.trustedparts.com
```

> Missing keys fall back to the seeded demo catalog automatically — the app is fully
> usable offline. Credentials are stored per-user and are never sent to the repository.

`config.toml` highlights:

| Section | Keys | Purpose |
| --- | --- | --- |
| `[defaults]` | `region`, `currency`, `site`, `language` | Localisation defaults |
| `[cache]` | `component_ttl_hours`, `snapshot_ttl_minutes`, `lifecycle_min_interval_hours` | Fact freshness windows |
| `[orchestrator]` | `timeout_seconds`, `max_retries`, `concurrency` | Live-source fan-out |
| `[rate_limits]` | `mouser_per_minute`, `digikey_per_minute`, `nexar_per_minute` | Outbound throttle per source (per-provider default rates for Arrow/Farnell/LCSC/TrustedParts are applied automatically) |
| `[api]` | `requests_per_minute`, `probe_mpn`, `probe_manufacturer` | Inbound rate limit & probe part |
| `[risk]` | `weight_*` | Risk-score weighting |

---

## 🌍 Deploy online (free — Render)

The repo ships a `render.yaml` Blueprint, so hosting on Render's free tier takes
about two minutes.

1. Push this repo to GitHub (it already lives at
   <https://github.com/KSHNKMRJHA/schemata>).
2. In the Render dashboard: **New + → Blueprint** → connect the `schemata` repo.
3. Render builds, starts the server and returns a public URL
   (`https://schemata-359w.onrender.com` unless you rename it).

**First visit** — the deployment generates a random API token automatically
(`SCHEMATA_ACCESS_TOKEN`, visible in the service's **Environment** tab). Open
the URL in a browser — a **login page** appears. Paste the token and click
**Access Schemata** to start. A session cookie is set for 7 days.

You can also authenticate by appending the token as a query parameter:

```
https://<your-app>.onrender.com/?token=<SCHEMATA_ACCESS_TOKEN>
```

This is useful for programmatic access (curl, API clients). The token accepts
its raw form — `+` and `=` characters need no URL encoding.

**Add live data keys** — in the same Environment tab, add the source keys you
want (see [Live Web](#-live-web-schemata-on-render) for the full list), then
**Manual Deploy**.

**Free-tier caveats**
- The instance **sleeps after ~15 min idle** — the first request after a nap
  takes ~1 minute (cold start).
- Storage is **ephemeral**: the SQLite database and cached reports live on the
  instance's disk and reset on restart/redeploy. Source keys set as env vars
  survive; long-term data persistence would use a managed Postgres/volume.

---

## 🌐 Live Web — Schemata on Render

A public instance runs on Render's free tier:

**<https://schemata-359w.onrender.com>**

### First visit

1. Open the URL — you'll see a **login page**.
2. Paste the shared access token (ask the deployer for it) and click **Access Schemata**.
3. A session cookie is set for 7 days; the whole app (search, BOM IQ, part dossier) works normally.

> Tokens with `+` or `=` characters (common in base64) paste directly into the form — no URL encoding needed.

### What works on the live instance

| Feature | Status |
| --- | --- |
| Part search (MPN lookup) | works — uses Offline Catalogue unless API keys are configured |
| Part dossier (lifecycle, risk, alternatives) | works — same as above |
| BOM upload & analysis (CSV / TXT / XLSX) | works |
| BOM export (HTML / JSON / CSV) | works |
| BOM IQ dashboard | works |
| Provider settings (add/remove keys) | works — keys persist per session |
| Demo catalog (zero-config) | always available as fallback |

### Adding live distributor data on Render

In the Render dashboard → **Environment** tab, add the keys you want:

```
NEXAR_CLIENT_ID=             NEXAR_CLIENT_SECRET=
DIGIKEY_CLIENT_ID=           DIGIKEY_CLIENT_SECRET=
MOUSER_API_KEY=
ARROW_API_KEY=
FARNELL_API_KEY=
LCSC_API_KEY=                LCSC_API_SECRET=
TRUSTEDPARTS_API_KEY=
```

Then click **Manual Deploy**. The keys appear in *Settings → Providers* in the app.

### Free-tier caveats

- **Sleeps after ~15 min idle** — first request after a nap takes ~1 min (cold start).
- **Ephemeral storage** — SQLite database and cached reports reset on restart/redeploy.
- API keys survive as env vars; long-term data persistence requires a managed Postgres or volume.

---

## 🛠️ Development

```powershell
.\.venv\Scripts\python -m pytest          # 47 tests
.\.venv\Scripts\python -m ruff check app ui tests
```

## 📦 Building the binary (Windows)

```powershell
# 1) Standalone exe  -> dist\Schemata.dist\Schemata.exe
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_nuitka.ps1

# 2) Inno Setup installer -> dist\installer\Setup_Schemata_v2.3.0.exe
& "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\Schemata-setup.iss
```

Requires `Nuitka` (in `[dev]` extras), a MinGW64 toolchain, and **Inno Setup 6**.

---

## 🔌 API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/healthz` | Health check (always 200) |
| `GET` | `/` | Root — redirects to `/parts` (or `/login` when token gate is active) |
| `GET` | `/login` | Token login page (no token needed to render) |
| `POST` | `/login` | Validate token, set session cookie, redirect to requested page |
| `GET` | `/api/parts?limit=` | Recently seen components (1–500) |
| `GET` | `/api/parts/{mpn}` | Full part dossier (JSON) |
| `GET` | `/api/sources` | Configured live/mock source status |
| `GET` | `/api/providers` | Provider list with credential status and badges |
| `POST` | `/api/settings` | Persist credential overrides |
| `POST` | `/api/settings/test` | Probe configured live sources |
| `POST` | `/api/part` | Single-part lookup (`?mpn=…`) |
| `POST` | `/api/search` | Keyword search across configured providers |
| `POST` | `/api/analyse` | BOM analysis (JSON lines array) |
| `POST` | `/bom/analyze` | Upload & analyze a BOM (multipart form) |
| `GET` | `/bom/export/{token}.{html,json,csv}` | Export a BOM report |
| `GET` | `/bom-iq/` | BOM IQ dashboard |
| `GET` | `/parts` | Parts list page |

All `/api/*` endpoints are rate-limited (`[api] requests_per_minute`, default 600/min).
When `SCHEMATA_ACCESS_TOKEN` is set (Render deployment), all routes except `/healthz`,
`/login` and `/static/*` require a valid token (cookie, `?token=`, or `Authorization: Bearer`).

---

## 🗂️ Project layout

```
app/
  __init__.py         Package root (__version__)
  main.py             FastAPI app, lifespan, token middleware, page routes
  config.py           BASE_DIR / DATA_DIR / UI_DIR paths
  sources/            Legacy source adapters (now wrapped by bomiq/providers)
  service.py          Report pipeline, catalogue service
  engine/
    parts_bridge.py   Legacy lookup bridge (source_status, lookup_part)
    routes.py         /api/part, /api/search, /api/analyse, /api/providers
    bomiq/
      config.py       ProviderSpec, credentials, settings, describe_providers
      engine.py       BOM analysis engine (orchestrates providers)
      version.py      BOM IQ version
      providers/
        registry.py   ProviderRegistry — parallel fan-out, merge, self_test
        base.py       BaseProvider, rate-limit, merge_parts
        mock.py       Offline demo catalogue
        mouser.py     Mouser Search API
        digikey.py    DigiKey Product Information (OAuth2)
        nexar.py      Nexar / Octopart GraphQL
        arrow.py      Arrow Electronics
        farnell.py    Farnell / element14
        lcsc.py       LCSC Electronics (keyless public endpoints)
        trustedparts.py TrustedParts aggregator
ui/
  templates/
    base.html         Base layout (topbar, footer, head)
    index.html        Home / landing page
    login.html        Token login page
    parts.html        Parts list
    search.html       Part search (legacy)
    bom.html          BOM upload & report
    report.html       BOM report view
    bom-iq.html       BOM IQ dashboard
  static/
    css/style.css     Application styles
    js/               Vanilla JS (app logic, charts)
render.yaml           Render Blueprint (free-tier deployment)
pyproject.toml        Package metadata, deps, entry points
config.toml           Runtime tunables (cache, rate limits, risk weights)
.env.example          Credentials template
packaging/
  build_nuitka.ps1    Nuitka standalone build
  Schemata-setup.iss  Inno Setup installer script
  launcher.py         Tk desktop launcher
tests/
  test_token_gate.py  Auth gate + login flow (13 tests)
  test_service.py     Service / report tests
  test_api.py         API integration tests
  ...                 Unit tests for engine, BOM, providers
```

---

## 🙌 Credits

Built and maintained by **Kishan J.**
- GitHub: [KSHNKMRJHA](https://github.com/KSHNKMRJHA)

Powered by **Mouser API Hub**, **DigiKey Product Information API**, **Nexar (Octopart) GraphQL**, **Arrow Electronics**, **Farnell / element14**, **LCSC Electronics** and **TrustedParts** data where configured.

---

## 📬 Contact

- **Issues & feature requests:** open a [GitHub issue](../../issues)
- **Questions / feedback:** [KSHNKMRJHA](https://github.com/KSHNKMRJHA)

---

## 📄 License

[MIT](LICENSE) © 2026 Kishan J.