# Changelog

## 1.0.0

First release.

**Ingestion**
- Readers for csv, tsv, txt, xlsx, xlsm, xls, ods, json, xml, html — including
  a pure-standard-library xlsx reader and an ods reader
- Encoding and delimiter sniffing; UTF-16 without a BOM; cp1252; gb18030
- Header detection through title blocks, stacked headers, merged cells,
  section banners and total rows; multi-sheet workbooks combined when their
  layouts match
- Column mapping from ~500 header spellings, regex patterns, learned
  corrections and content inference, with a confidence and a reason per field
- Saved layout templates, replayed on an exact header fingerprint
- Designator range expansion, duplicate merging, DNP columns in either sense,
  per-column decimal-separator and currency detection
- 25 validation rules covering Excel damage, designator conflicts, quantity
  and value contradictions, and placeholder part numbers

**Providers**
- DigiKey (Product Information v4), Mouser (Search v1), Octopart/Nexar
  (Supply GraphQL, batched), Arrow (ItemService v4), Farnell/element14
  (Product Search, 30 regional stores), LCSC, TrustedParts
- Built-in offline catalogue: ~55 real part numbers plus deterministic
  synthetic data, clearly labelled everywhere it appears
- Per-provider rate limiting, retries with backoff, `Retry-After`, circuit
  breaker, two-level caching, credential validation and per-provider statistics

**Analysis**
- Match scoring with written reasoning; field auto-completion with provenance
- Six-factor risk scoring with obsolescence floors and catastrophic-factor
  floors
- MOQ/SPQ-aware costing, cheapest source per line, split baskets, price curves,
  like-for-like savings, and comparison against the prices in the source file
- Alternate scoring from four candidate sources, with hard ceilings for
  mismatched value, footprint or unverifiable fit
- RoHS/REACH/SVHC/halogen/origin/HTS/ECCN/ITAR checks against configurable
  rules
- Placement-weighted BOM health score with grades and named drivers

**Output**
- Nine-sheet Excel report written by a bundled standard-library xlsx writer
- Self-contained HTML report, interactive and printable
- CSV, issues CSV, distributor-ready quote CSV, full JSON and flat JSON
- Saved projects in a local SQLite database

**Application**
- Desktop app with a native window and browser fallbacks
- Single-page UI: dashboard, BOM grid, risk matrix, sourcing, alternates,
  compliance, findings, part search, detail drawer, settings, exports
- Light and dark themes, both explicitly designed and colour-validated
- Full CLI with CI exit codes, and a loopback REST API with token auth,
  `Host`/`Origin` checks and body caps
- PyInstaller packaging for Windows, macOS and Linux
- 369 tests, no network and no API keys required

**Correctness pass before release**

A hostile read of the whole codebase found 21 defects that the feature tests
did not catch. All are fixed, and `tests/test_regressions.py` now pins each
one so a future refactor that reintroduces it fails with an explanation of
what the bug did.

The ones that silently produced *wrong numbers* rather than errors:

- A BOM carrying its own build quantity overwrote the shared settings, so
  every later analysis in the session was costed at that quantity
- A notes row or a blank gap in the middle of a table ended the table, so the
  lines after it were dropped without a word; the excluded count is now
  reported as a warning
- The xlsx reader read cell text on the parser's `start` event, so values
  whose text node straddled a read boundary came back blank in large workbooks
- Merged cells in ods files did not hold their column, shifting every value
  after them one column left
- Split baskets quoted every leg at the full quantity's price break instead of
  its own, and quoted sources below their minimum order
- A unit price of exactly 0 ("call for pricing") was costed as free, so an
  unquotable line reported as fully covered
- An aggregator calling DigiKey "Digi-Key" double-counted the same stock and
  turned a single-sourced part into a multi-sourced one
- A quantity column in European notation read `1.000` as 1
- Broker-only availability *lowered* the risk score
- `0.55 × 100` ceiled to 56 in binary floating point
- `0 Ω` jumpers crashed the value-contradiction rule
- A part with no data anywhere scored as low risk when the weights summed
  to zero

And the ones that were reachable from outside: an upload filename with path
separators, a non-numeric `limit` or `quantity` on the API, and hostile
settings values all returned 500s; exported CSV did not neutralise cells
starting with `=`, `+`, `-` or `@`; changing a provider's rate limit at
runtime reset its token bucket to full.
