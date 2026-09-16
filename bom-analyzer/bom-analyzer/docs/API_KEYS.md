# Getting the API keys

BOM-IQ works without any keys using its offline catalogue, but the offline
figures are synthetic placeholders. For real pricing, stock and lifecycle data
you need at least one live provider.

**If you only set up one, make it Nexar.** One call returns lifecycle status,
aggregated availability across ~150 distributors, median pricing, parametrics,
compliance attributes and similar parts — which is most of what BOM risk
analysis needs.

**If you only set up two, add Mouser.** The key is a single string, the signup
takes about five minutes, and it gives you a second independent opinion on
lifecycle status, which is exactly what the "sources disagree" warning needs.

---

## Where keys are stored

In the OS credential store when the optional `keyring` package is installed
(Windows Credential Manager, macOS Keychain, Secret Service on Linux),
otherwise in `credentials.json` in the config folder with owner-only file
permissions. They are never written to logs, never returned by the API, and
never included in a report — everything the UI shows is masked
(`abc****xyz`).

Three ways to set them:

```bash
# 1. the CLI
bomiq providers --set nexar.client_id=… nexar.client_secret=… --enable nexar

# 2. the UI:  Settings -> Providers -> paste -> Save key -> Test connection

# 3. environment variables (useful in CI; these always win)
export NEXAR_CLIENT_ID=… NEXAR_CLIENT_SECRET=…
```

A `.env` file in the working directory, next to the executable, or in the
config folder is also read.

Check everything at once:

```bash
bomiq providers --test
```

---

## Octopart / Nexar — *aggregator, start here*

1. Go to <https://portal.nexar.com/> and create an account.
2. Create an **application**. Choose the **Supply** scope.
3. Copy the **Client ID** and **Client Secret**.

```bash
bomiq providers --set nexar.client_id=YOUR_ID nexar.client_secret=YOUR_SECRET \
                --enable nexar
```

Notes:

- OAuth2 client credentials against `identity.nexar.com`; tokens last ~24 h
  and BOM-IQ caches them in memory.
- Nexar bills per part queried, so BOM-IQ batches up to 20 MPNs per GraphQL
  request and caches results for six hours by default
  (Settings → Ingestion → cache lifetime).
- The free tier has a monthly part-query allowance. A 200-line BOM with 60
  unique parts costs 60 queries, not 200.

## Mouser — *simple key, good second source*

1. Go to <https://www.mouser.com/api-hub/> and request a **Search API** key.
2. You will receive a single key string by e-mail, usually the same day.

```bash
bomiq providers --set mouser.api_key=YOUR_KEY --enable mouser
```

Notes:

- The free tier allows roughly 1000 calls/day and 30/minute, so BOM-IQ
  throttles to 1.5 requests/second by default.
- Mouser reports errors *inside* a 200 response; BOM-IQ detects an invalid key
  from that and tells you plainly rather than silently returning nothing.

## DigiKey — *deepest parametrics and compliance*

1. Go to <https://developer.digikey.com/> and create an account.
2. Create an **organisation**, then an **application**.
3. Subscribe the application to **Product Information V4**.
4. Copy the **Client ID** and **Client Secret**.

```bash
bomiq providers --set digikey.client_id=YOUR_ID \
                     digikey.client_secret=YOUR_SECRET --enable digikey
```

Notes:

- OAuth2 client credentials; the token lasts ~10 minutes and is refreshed
  automatically under a lock, so concurrent workers only fetch it once.
- Optional: set `digikey.customer_id` to get your contract pricing instead of
  list pricing.
- To test against the sandbox host first, enable
  Settings → Analysis → *DigiKey sandbox* (or `bomiq config set
  digikey_sandbox=true`).

## Arrow

1. Register at <https://developers.arrow.com/>.
2. Request access to the **ItemService** API.

```bash
bomiq providers --set arrow.api_key=YOUR_KEY arrow.login=you@example.com \
                --enable arrow
```

Arrow's response schema has changed between revisions, so BOM-IQ parses it
defensively — it looks for the documented containers and falls back to a
recursive scan for anything carrying a manufacturer part number. If Arrow
renames a wrapper key, the adapter keeps working.

## Farnell / element14 / Newark

1. Register at <https://partner.element14.com/>.
2. Create an application to get a **Product Search API** key.

```bash
bomiq providers --set farnell.api_key=YOUR_KEY farnell.store=uk.farnell.com \
                --enable farnell
```

The **store** decides both the catalogue and the currency. Common values:

| Store | Region | Currency |
|---|---|---|
| `uk.farnell.com` | UK | GBP |
| `de.farnell.com` | Germany | EUR |
| `in.element14.com` | India | INR |
| `sg.element14.com` | Singapore | SGD |
| `www.newark.com` | USA | USD |
| `canada.newark.com` | Canada | CAD |

Leave it blank and BOM-IQ picks one from Settings → Analysis → *Sourcing
region*.

## LCSC — *low-cost APAC sourcing*

LCSC has no long-term stable public API. Two transports are supported:

- **Partner API** — set `lcsc.api_key`, `lcsc.api_secret` and the environment
  variable `BOMIQ_LCSC_BASE_URL` to the endpoint LCSC gave you.
- **Public catalogue endpoints** — work without a key, but are undocumented,
  rate limited and can change without notice.

The provider is disabled by default, and every record it returns through the
public path is marked *unofficial source — verify before committing a
purchase order*.

## TrustedParts — *authorised-distributor stock only*

TrustedParts (an ECIA initiative) aggregates inventory from authorised
distributors only, which makes it useful for hunting a scarce part without
straying into the grey market. There is no openly published REST contract, so
this adapter targets the partner JSON feed and is configurable:

```bash
export BOMIQ_TRUSTEDPARTS_BASE_URL=https://the-endpoint-they-gave-you/v1
bomiq providers --set trustedparts.api_key=YOUR_KEY --enable trustedparts
```

If the response shape does not match, BOM-IQ reports it clearly and carries on
with the other providers rather than failing the analysis.

---

## Choosing which to enable

| Goal | Enable |
|---|---|
| Obsolescence and risk review | Nexar, plus Mouser or DigiKey for a second opinion |
| Accurate US purchasing | DigiKey + Mouser |
| EU / UK purchasing | Farnell (`uk.farnell.com` or `de.farnell.com`) + Nexar |
| India | Farnell (`in.element14.com`) + Nexar + Mouser |
| Cost-down sourcing | Nexar + LCSC |
| A scarce part, authorised only | TrustedParts + Nexar |

More providers means better coverage but more calls and more time. Settings →
Ingestion → *concurrent lookups* controls parallelism; the per-provider rate
limits are applied independently so one slow provider does not hold the others
back.

---

## Rate limits and caching

Each provider has its own token-bucket rate limit and its own circuit breaker:
after six consecutive failures a host is skipped for a minute so a provider
having a bad day does not slow the whole run to a crawl.

Responses are cached in the local SQLite database for six hours by default.
That makes re-running the same BOM nearly instant and keeps you well inside
any quota while you are iterating on a design.

```bash
bomiq cache stats     # how much is cached
bomiq cache prune     # drop expired entries
bomiq cache clear     # start fresh (forces live calls)
```

---

## Troubleshooting

| Symptom | What it means |
|---|---|
| `Provider is enabled but the … is not set` | the key was never stored — check `bomiq providers` |
| `rejected the credentials (HTTP 401/403)` | wrong key, or the app is not subscribed to the right API product |
| `did not return an access token` | client ID/secret mismatch, or the wrong scope on the application |
| `rate/quota limit reached` | you have hit the daily or per-minute cap; the cache will carry you until it resets |
| `Skipping host: too many consecutive failures` | the circuit breaker opened; it retries automatically after a minute |
| Everything says "offline catalogue" | no provider is both selected *and* configured — `bomiq doctor` will say which |
