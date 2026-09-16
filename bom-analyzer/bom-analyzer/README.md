# BOM-IQ

**A local BOM analysis and part-intelligence engine for electronics BOMs.**

Drop in a bill of materials in almost any format. BOM-IQ works out which
column is which, cleans and validates the data, looks every part up across
DigiKey, Mouser, Octopart/Nexar, Arrow, Farnell/element14, LCSC and
TrustedParts in parallel, then tells you what it will cost, what will bite you,
and what to use instead.

Everything runs on your machine. The only thing that leaves it is the part
numbers you ask the providers about.

---

## What it actually does

**Reads any BOM.** `.csv .tsv .txt .xlsx .xlsm .xls .ods .json .xml .html` —
with encoding and delimiter sniffing, title blocks above the table, stacked
headers, merged cells, section banners, total rows, multi-sheet workbooks and
European decimal commas all handled. Columns are matched by name, by pattern
and by what the values actually look like; confirm a layout once and it is
remembered for next time.

**Cleans and checks it.** Designator ranges expanded (`R1-R8`), duplicate
lines merged with quantities summed, DNP columns in either sense understood,
and 25 validation rules that catch what really goes wrong: part numbers Excel
mangled into scientific notation, `#N/A` left in a cell, a designator on two
different parts, a quantity that disagrees with the designator count, values
that contradict the description.

**Enriches it.** One consolidated record per part, merged across every
provider that answered: lifecycle status, stock at each distributor, full
price-break ladders, MOQ and pack size, lead times, parametrics, datasheets,
RoHS/REACH/HTS/ECCN and published substitutes. Worst lifecycle wins when
sources disagree, because obsolescence reported by one distributor is real
even if another still says Active.

**Scores the risk.** Six weighted factors per line — lifecycle, availability
against *your* build quantity, multi-sourcing, lead time, compliance and data
quality — each with a written explanation, rolled up into one BOM health
score out of 100. NRND or worse can never read as "Low", however cheap and
plentiful the part looks today.

**Costs it properly.** Price breaks, minimum order quantity and standard pack
size all honoured, so a part you need 37 of but which ships in reels of 4000
costs a reel and the report says so. Cheapest source per line, a split basket
when no single distributor can cover the quantity, a price curve from 1 to
10,000 units, and a comparison against the prices already in your file.

**Finds alternates.** From your own approved-alternates column first, then
manufacturer and distributor substitution data, then aggregator similarity,
then parametric search. Each one scored out of 100 on lifecycle,
availability, form/fit, compliance and price, with the reasons it fits *and*
the concerns that remain. A different footprint or a different electrical
value is capped hard — the tool will not pretend a 0.1 µF is a substitute for
a 10 µF.

**Checks compliance.** RoHS, REACH/SVHC, halogen-free, country of origin, HTS
code, ECCN and ITAR per line, with your own rules deciding what blocks a
release. "Unknown" is reported as unknown, never as compliant.

---

## Install and run

BOM-IQ needs **Python 3.9 or newer and nothing else**. The engine, the CLI,
the local server and the UI are all standard library.

```bash
# desktop app
python run.py

# or the command line
python run.py --cli analyse my-bom.xlsx --build 500 --export xlsx,html
```

The first run creates a data folder for the local database, logs and exports
(`%LOCALAPPDATA%\BOM-IQ` on Windows, `~/Library/Application Support/BOM-IQ`
on macOS, `~/.local/share/bomiq` on Linux).

### Optional extras

None of these are required; each one adds speed or a format.

```bash
pip install -r requirements-optional.txt
```

| Package | What it adds |
|---|---|
| `openpyxl` | richer `.xlsx` reading (a built-in reader is used without it) |
| `xlrd` | legacy `.xls` (Excel 97–2003) |
| `rapidfuzz` | ~50× faster fuzzy matching on large BOMs |
| `keyring` | store API keys in the OS credential store instead of a file |
| `pywebview` | a native desktop window instead of a browser window |

### Build a packaged app

```bash
# Windows
packaging\build_windows.bat        ->  dist\BOM-IQ\BOM-IQ.exe

# macOS / Linux
./packaging/build_unix.sh          ->  dist/BOM-IQ.app  |  dist/BOM-IQ/BOM-IQ
```

Both scripts create a build virtualenv, install PyInstaller and the optional
extras, run the test suite, and package. See `docs/PACKAGING.md` for signing,
notarisation and one-file builds.

---

## Without API keys

BOM-IQ works immediately with a built-in **offline catalogue**: ~55 real,
widely-used part numbers plus deterministic synthetic data for anything else.
The whole pipeline runs — validation, matching, costing, risk, alternates,
compliance, exports — so you can evaluate the tool and run the tests on an
air-gapped machine.

Offline figures are clearly labelled as synthetic everywhere they appear: a
banner in the UI, a warning on the analysis, a note on every offer, and a
line in the Excel summary. They are not live distributor data and must not be
used for purchasing.

Add a key and the same pipeline runs against the real thing. See
**[docs/API_KEYS.md](docs/API_KEYS.md)** for how to get each one — Mouser and
Nexar take about five minutes each and Nexar is the highest-value single
provider for risk work.

```bash
python run.py --cli providers --set mouser.api_key=YOUR_KEY --enable mouser
python run.py --cli providers --test
```

---

## The desktop app

| Tab | What it is for |
|---|---|
| **Dashboard** | health score, key figures, distributions, "look at these first" |
| **BOM** | every line with everything learned; filter, sort, click for detail |
| **Risk** | one column per risk factor, so you can see *why* a line scores |
| **Sourcing** | every offer costed for this build, cheapest first, with buy links |
| **Alternates** | scored substitutes with reasons and concerns |
| **Compliance** | RoHS/REACH/origin/HTS/export per line, plus the rules applied |
| **Findings** | every validation finding with a suggested action |
| **Part search** | look a part up directly, before it ever reaches a BOM |

Click any line for the detail drawer: match reasoning, risk factors with
scores, the full offer table, a price curve, the catalogue record and
parametrics, compliance, and alternates you can search again on demand.

Light and dark themes, keyboard shortcuts (`Ctrl/Cmd-K` to filter the BOM,
`Esc` to close), and a column-mapping editor for when the automatic mapping
needs a nudge.

---

## Command line

```bash
bomiq analyse BOM.xlsx --build 500 --export xlsx,html --out reports/
bomiq analyse BOM.xlsx --fail-on high --min-health 70      # CI gate
bomiq inspect BOM.csv                    # show the mapping, analyse nothing
bomiq part LM358DR                       # one part, all providers
bomiq search "0603 10k 1% resistor"
bomiq providers --test                   # check every key
bomiq config set currency=EUR build_quantity=1000
bomiq projects list
bomiq serve --port 8756                  # UI + REST API
bomiq doctor                             # environment diagnostics
```

Exit codes make it usable in a pipeline: `0` success, `2` error, `3` health
below `--min-health`, `4` lines at or above the `--fail-on` risk level, `5`
findings at or above the `--fail-on` severity.

Export formats: `xlsx` (nine-sheet workbook), `csv`, `issues-csv`,
`quote-csv` (ready to paste into a distributor BOM tool), `json`,
`json-flat`, `html` (self-contained, printable).

---

## REST API

`bomiq serve` exposes the same engine over loopback JSON. Every mutating call
needs the session token printed at start-up; requests from other origins and
non-loopback addresses are refused.

```
GET  /api/bootstrap                       everything the UI needs
POST /api/upload                          upload a BOM, get the mapping preview
POST /api/remap                           re-ingest with a corrected mapping
POST /api/analyse                         start a job -> 202 + job id
GET  /api/jobs/{id}                       progress
GET  /api/jobs/{id}/result                the finished analysis
GET  /api/analysis/{id}/export?format=…   download a report
GET  /api/part?mpn=…                      single lookup
POST /api/search                          keyword search
GET  /api/projects                        saved analyses
```

Full list in `docs/API.md`.

---

## How it is put together

```
bomiq/
  util/        text, units, money, HTTP (retry, rate limit, circuit breaker)
  core/        domain model, SQLite cache/templates/projects, errors
  ingest/      readers · header detection · column mapping · normalise
               · dedupe · 25 validation rules · pipeline
  providers/   base (auth, cache, stats) + 7 live providers + offline catalogue
  analysis/    match · risk · cost · alternates · compliance · health
  export/      xlsx writer (stdlib) · Excel report · CSV/JSON · HTML report
  server/      stdlib HTTP router · jobs · REST API · single-page UI
  engine.py    the orchestrator
  cli.py       command line
  desktop.py   native window, with browser fallbacks
```

Design decisions worth knowing:

- **Standard library only.** A packaged desktop app that depends on nothing
  cannot break because a wheel failed to build. Optional accelerators are
  detected at runtime and every path has a fallback — including a pure-stdlib
  XLSX reader *and* writer, so reports look identical on every machine.
- **Degrades, never fails.** A provider outage, a rejected key, a malformed
  API response or an exception inside one line is captured and reported
  against that line. The rest of the BOM still completes.
- **Every number is explained.** Match confidence, each risk factor, each
  alternate's score and each cost figure carry the reasoning that produced
  them. A buyer should never have to trust an unexplained number.
- **Nothing is silently assumed.** Unknown compliance is unknown. An
  unverifiable alternate is capped. An estimated price is labelled. An
  offline run says so on every screen and in every export.

---

## Testing

```bash
python -m unittest discover -s tests          # 369 tests, ~6 seconds
python samples/make_samples.py                # regenerate the sample BOMs
```

The live providers are tested against recorded response payloads through a
fake transport, so the whole suite runs with no network and no API keys.
`samples/` holds seven deliberately awkward BOMs — an Altium export with a
title block, a hand-made sheet with European decimals and an inverted fit
column, an SAP extract with only internal part numbers, a file Excel has
damaged, a multi-sheet workbook, a JSON BOM and a semicolon-delimited German
export — and every one of them is asserted end to end.

---

## Where your data lives

| What | Where |
|---|---|
| Database (cache, templates, saved projects) | data folder / `bomiq.sqlite3` |
| API keys | OS keyring, else `credentials.json` with owner-only permissions |
| Settings | config folder / `settings.json` |
| Logs (credentials redacted) | data folder / `logs/` |
| Exports | data folder / `exports/`, or wherever you point `--out` |

`bomiq doctor` prints all of these. Nothing is uploaded anywhere; the network
is used only for the provider lookups you configure.
