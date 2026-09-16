# Quick start

## 1. Run it

```bash
python run.py
```

A window opens. If no native window toolkit is available, BOM-IQ falls back to
a chromeless Chrome/Edge window, and then to your default browser — the URL is
always printed in the terminal.

## 2. Try it with no setup

Click **Load a sample BOM**. That runs the whole pipeline on a 14-line sensor
board using the built-in offline catalogue, so you can see every screen
working before touching an API key. The figures are synthetic and labelled as
such.

## 3. Analyse your own BOM

Drag the file onto the window, or click **Choose a file…**.

You land on the **column mapping** screen. Check the fields it found — each
one shows a confidence and the reason it was chosen. Anything wrong, change it
from the dropdown and press **Apply**. Set the **build quantity** (this drives
stock checks, price breaks and MOQ) and press **Analyse BOM**.

Once a layout is confirmed, it is saved. The next export from the same tool
maps itself.

## 4. Read the result

Start on the **Dashboard**: the health score, the key figures, and *"Look at
these first"* — the highest-risk lines with the reason each one scores.

Then click any line, anywhere, for the full detail: why it matched, each risk
factor and its score, every offer with stock and price breaks, a price curve,
the catalogue record, compliance, and scored alternates.

## 5. Get real data

Settings → Providers. Nexar and Mouser are the two quickest wins; see
[API_KEYS.md](API_KEYS.md). Paste a key, press **Save key**, then **Test
connection**. Press **Re-run** in the top bar and the same BOM is analysed
against live data.

## 6. Export

**Export ▾** in the top bar:

| Format | Use it for |
|---|---|
| `xlsx` | the full nine-sheet workbook — this is the deliverable |
| `html` | self-contained, printable; good for e-mail and design reviews |
| `csv` | the enriched BOM as one flat table, for ERP or a script |
| `quote-csv` | MPN / manufacturer / quantity, ready to paste into a distributor BOM tool |
| `json` | the complete analysis; reopens in BOM-IQ and round-trips exactly |

**Save as project** keeps the analysis in the local database so you can reopen
it from the start screen and compare later.

---

## In a build pipeline

```bash
bomiq analyse hardware/BOM.xlsx \
      --build 2000 \
      --export xlsx,html --out artifacts/ \
      --fail-on high --min-health 70
```

Fails the build if any line is at high or critical supply risk, or if BOM
health drops below 70. Exit codes: `3` = health gate, `4` = risk gate,
`5` = findings gate, `2` = error.

## A nightly obsolescence check

Run the same command on a schedule and diff the JSON:

```bash
bomiq analyse BOM.xlsx --export json --out /var/lib/bom-checks/$(date +%F)
```

Cached provider data expires after six hours, so a nightly run always sees
fresh lifecycle status.
