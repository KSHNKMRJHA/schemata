# Design notes

Why the code looks the way it does. Useful if you are extending it.

## Zero required dependencies

A packaged desktop app that depends on nothing cannot break because a wheel
failed to build on someone's machine, and cannot drift because a transitive
dependency shipped a breaking minor. So the engine, the CLI, the HTTP server
and the UI are standard library only, including:

- **an XLSX reader** (`zipfile` + `ElementTree`, streaming with `iterparse`),
  so `.xlsx` works with no `openpyxl`;
- **an XLSX writer** (`bomiq/export/xlsx_writer.py`) with fonts, fills,
  borders, number formats, frozen panes, autofilter, merges and hyperlinks —
  so the Excel deliverable is byte-identical on every machine;
- **an HTTP client** (`urllib`) with token-bucket rate limiting, jittered
  exponential backoff, `Retry-After` handling, a circuit breaker and response
  caching;
- **an HTTP server** (`ThreadingHTTPServer`) with a pattern router, a
  multipart parser, and loopback/origin/`Host` checks.

Optional packages (`openpyxl`, `xlrd`, `rapidfuzz`, `keyring`, `pywebview`)
are detected at import time and used when present. Every one has a fallback,
and the test suite passes with none of them installed.

## Providers are synchronous; the registry is concurrent

Each provider is a plain synchronous class. Concurrency lives in
`ProviderRegistry`, which fans out over a thread pool. This keeps provider
code trivial to read and to test, and matches what distributor APIs reward: a
small number of steady connections rather than a burst.

Batch endpoints are used where they exist. Nexar bills per part queried, so
`fetch_many` sends up to 20 MPNs per GraphQL request; the registry calls it
first and only fans the remainder out one-by-one.

`lookup_many` deduplicates by normalised MPN, so a 200-line BOM with 60 unique
parts makes 60 lookups.

## Everything degrades

The engine is built so that no single failure loses the run:

| Failure | What happens |
|---|---|
| Provider has no key | recorded in `setup_errors`, surfaced as a warning |
| Provider returns 401/403 | provider disabled for the run, reported per line |
| Provider times out or 5xx | retried with backoff, then the circuit opens |
| Provider returns unreadable JSON | captured as a provider error on that line |
| One line raises an exception | that line gets an error issue; the rest complete |
| The local database is corrupt | rotated aside and recreated |
| The UI throws | progress and analysis are unaffected; it is a separate process boundary |

## Every number carries its reasoning

`Match.reasons`, `RiskFactor.detail`, `Alternate.reasons` / `.concerns`,
`SourcingOption.notes` and `Cost.notes` are populated as the decision is made,
not reconstructed afterwards. This is why the UI can explain a score and why
the Excel report has a "Why" column.

It also disciplines the scoring: if a factor cannot be explained in a sentence,
it probably should not be in the score.

## Scoring has floors and ceilings, not just averages

A weighted average hides exactly the things a buyer needs to see, so the
scoring applies hard bounds:

- **Risk floors.** NRND ≥ 30, EOL ≥ 52, obsolete ≥ 72, no catalogue data ≥ 45.
  A part that is end-of-life can never read as "Low" because it happens to be
  cheap and in stock today.
- **Catastrophic-factor floors.** Any single factor at 95+ forces the line to
  at least 72.
- **Alternate ceilings.** A different electrical value caps the score at 30; a
  different footprint at 52; an unverifiable fit at 58–62. The tool never
  presents an unqualified recommendation it cannot justify.
- **Health penalties.** Obsolete parts, no-stock lines, unmatched lines and
  compliance blockers subtract points directly, so they cannot be averaged
  away by a long tail of healthy passives.

## Costing is like-for-like

Two traps, both avoided deliberately:

1. **MOQ and pack size are part of the price.** A part you need 37 of that
   ships in reels of 4000 costs a reel. `order_quantity()` computes the
   purchasable quantity and the overbuy is reported, not hidden.
2. **Extended totals are not comparable across offers.** A 4000-piece reel
   always "costs more" than 500 of cut tape, so savings and price spread are
   computed from **unit price at the required quantity**, never from the
   difference between extended totals. Counting that gap as a saving would
   invent money that was never on the table.

## Ingestion is a pipeline with an escape hatch at every stage

```
read → pick sheet → detect header → map columns → normalise
     → merge duplicates → validate
```

Each stage records what it did and can be overridden: force a sheet, force a
header row, pin a column. The UI exposes all three, because automatic mapping
is right most of the time and the remaining cases need a human, not a better
heuristic.

Confirmed mappings are learned two ways: the whole layout is fingerprinted
(sorted set of normalised headers) and replayed on an exact hit, and individual
`header → field` confirmations are counted so even a new layout benefits from
past corrections.

## Locale decisions are made per column, not per cell

`0,029` on its own is ambiguous — 29 thousandths or twenty-nine? A *column* is
not ambiguous: if any cell has a comma followed by one, two, or four-or-more
digits and no cell uses a period as a decimal point, the column is European.
`detect_decimal_comma()` decides once for the column and every cell is parsed
with that answer. The same idea supplies a currency from a header like
`Rate (EUR)` when the cells do not carry one.

## Charts

Hand-drawn inline SVG, no chart library. The colour work follows the project's
data-viz system: status colours are reserved (good / warning / serious /
critical), always paired with a text label, and the palette was run through a
CVD separation validator.

That validation is why the distributions are **labelled bar rows rather than
stacked bars**: in the status ramp, warning and serious sit at ΔE 13.6, below
the 15 floor for normal-vision separation, and they would be adjacent segments
in a lifecycle or risk stack. One directly-labelled row per category has no
adjacent pairs at all, and reads better in a sidebar anyway.

Both themes are *selected*, not flipped: each mode declares its own surfaces
and its own steps.

## Security posture

It is a local tool, but the basics still matter, because a web page open in
the user's browser can otherwise reach `localhost`:

- loopback-only bind, with non-loopback client addresses refused
- a session token required for every mutating call
- `Host` validation (DNS rebinding) and `Origin` rejection (CSRF)
- request body caps and static-path traversal checks
- credentials in the OS keyring when available, else `0600` file permissions
- a redacting log filter, so a key pasted into a debug log is still `***`

## Extending it

**A new provider**: subclass `Provider`, implement `fetch()`, register it in
`bomiq/providers/registry.py` and add a `ProviderSpec` in `bomiq/config.py`.
The base class gives you auth plumbing, rate limiting, retries, caching,
statistics and error classification. Add a recorded payload to
`tests/test_providers.py` and the fake transport tests it with no network.

**A new validation rule**: one decorated function in
`bomiq/ingest/validate.py`. It appears in the UI, the API (`/api/rules`), the
report and the per-line findings automatically.

**A new risk factor**: a function in `bomiq/analysis/risk.py` returning a
`RiskFactor`, added to the list in `score_line()`, plus a weight in
`Settings`. It flows into the per-line score, the health roll-up, the Risk tab
and the Excel report without further work.

**A new export format**: a `write_*(analysis, path)` function, registered in
the `EXPORTERS` maps in `bomiq/cli.py` and `bomiq/server/app.py` and listed in
`flat.iter_export_formats()`.
