# rigmark-data

The public data side of **My Rig** in Rigmark (Gaming Optimizer): the source allow-list, the pricing bot, and the
signed daily price file the app downloads.

> **Status (November 12, 2026): collection is disabled.** No request has been made to any shop. `sources/registry.json`
> ships with `"collectionEnabled": false` and every seller `"enabled": false`, `"status": "proposed - legal read pending"`.
> The collection workflow has no schedule and every job after the guard is skipped while collection is off.
> All tests run on hand-written fixture pages.

## What it publishes

One file, `price-file.json`, as a release asset of this repository. It holds exchange rates, a tax table, regional
street-price indices (layer 2) and market bands (layer 3). The app verifies its signature with a public key it ships,
refuses a file older than the one it has, and keeps working on its last good file when anything fails.

## Neutrality rules

The full rules live in the app repository (`docs/MY-RIG-NEUTRALITY.md`). The bot enforces its part:

- **Never one shop.** A published price is a median with its range across sellers. A market band needs at least
  **three** independent sellers; a regional index at least **two**. Sellers under common ownership count once
  (LDLC, Materiel.net and Top Achat are one seller). The file format has no field for a shop name, a link or one
  shop's price, and the app refuses unknown fields.
- **Labelled.** The app shows every price with its basis ("market band", "index-adjusted estimate", "launch-price
  estimate") and its data date. An index built partly from the hand seed is marked `seeded`.
- **Nothing for sale.** No affiliate links, tracking parameters, sponsored placement, paid ranking, paid service or paid API.
- **Evidence is private.** Each kept price keeps its link, time, currency, stock status and structured-data snippet.
  That evidence is encrypted with [age](https://age-encryption.org) before any upload; only medians and bands are public.

## Bot conduct

- Collects only from domains on the allow-list (`sources/registry.json`), each approved one by one by the owner.
- Plain HTTPS requests under its own name: `RigmarkPriceBot/0.1 (+https://github.com/Dreko-Coder/rigmark-data; ...)`.
  No browser imitation, no cookies, no logins, no proxies, no CAPTCHA solving, no retry from another address, no AI.
- Reads `robots.txt` first and obeys it, including a slower `Crawl-delay`. At least 10 seconds between requests to a
  domain, longer where the shop says so (Megekko: one request an hour, one request per run).
- 429: waits (Retry-After, else exponential) at most twice, then pauses the domain. 403: the domain is disabled for the
  run and reported. **A shop that blocks is never worked around.** Redirects to another host are not followed.
- Reads only schema.org Product/Offer structured data (JSON-LD, then microdata). A page without it is counted and skipped.
- A price is kept only if its GTIN (check digit verified) or part number matches exactly one part in the dataset,
  its currency is the market's, the item is new and in stock or orderable, and it was seen in the last 7 days.

## How it works

| Stage | Code | Notes |
|---|---|---|
| Registry and guard | `pricebot/registry.py` | allow-list, delays, `collectionEnabled` guard |
| Discover | `pricebot/discover.py` | sitemaps listed in robots.txt, URL pre-filter |
| Collect | `pricebot/fetch.py` | the only network code; refuses to exist while collection is disabled |
| Extract | `pricebot/extract.py` | JSON-LD and microdata Product/Offer: price, currency, availability, condition, MPN, GTIN |
| Match | `pricebot/match.py` | dataset part numbers and GTINs (`parts/*.json`, schema 1 from the app repo) |
| Dry-run report | `pricebot/pipeline.py` | per domain: pages, no structured data, matched; coverage per market |
| Bands and indices | `pricebot/compute.py` | medians, ranges, seller minimums, outliers (35%), stale (7 days), movement gate (25%) |
| Price file | `pricebot/pricefile.py` | payload, content rules (a port of the app's `PriceFile.Validate`), writer |
| Signing | `pricebot/signing.py` | OpenSSL ECDSA P-256 / SHA-256, DER signature over `"Rigmark.PriceFile.v1\n" + payload` |
| Evidence | `pricebot/evidence.py` | age encryption to the owner's recipient key |

The index multiplier is street price over the app's own launch-price estimate (the same conversion and tax rule as
the app's `BaselinePrice`), so the app can multiply it back. Rules marked Proposed in the plan (one price per seller
group: its lowest valid offer; at least two parts per index; 35% outlier; 25% movement; 7-day staleness) are
constants at the top of `pricebot/compute.py`.

A broken run keeps the last file: the new file is validated, signed and its signature checked again before it
atomically replaces the old one, and the workflow publishes only when every step passed.

Commands (`python -m pricebot ...`): `guard`, `dry-run --pages DIR` (offline, stored pages), `collect`, `fetch-fx`,
`build`, `encrypt-evidence`. Every command that would reach the network, sign or upload exits with code 2 while
collection is disabled.

## Tests

Python 3.12 standard library plus the `openssl` command line; the .NET 10 SDK for the conformance check; `age` for
the evidence test.

```
python -m unittest discover -s tests -v
```

Fixtures in `tests/fixtures` are hand-written (`.example` domains, a made-up ECB file, a small parts set). The test
helper blocks sockets, so no test can reach any site. Signing tests use a throwaway key made at test time.
`conformance/PriceFileCheck` verifies the bot's output with the same .NET calls the app makes
(`ImportSubjectPublicKeyInfo`, `VerifyData(..., SHA256, Rfc3279DerSequence)`, camelCase JSON with unknown fields refused).
The `tests` workflow runs all of this on every pull request on GitHub-hosted runners (free in a public repository).

## Enabling collection (Andy's steps)

Nothing below has been done. In order:

1. **Legal read** of the nine proposed sellers (`sources/registry.json`: terms and robots notes per domain). Three carry
   a general no-reproduction clause, and EU/UK database right applies to systematic extraction; written permission
   is the clean route there. Record the outcome per seller.
2. **Parts dataset.** Put the app's `data/rig/parts/*.json` in `parts/` (or decide another way for the bot to read it;
   publishing it here makes the curated dataset public).
3. **Evidence key.** Run `age-keygen -o rigmark-evidence.key` on your own PC, keep the file offline, and put the
   `age1...` public key in `sources/evidence-recipient.txt` in place of the placeholder.
4. **Approve sellers.** In a reviewed pull request set `"enabled": true` on each approved seller and its domains, and
   change its `status`. Then set `"collectionEnabled": true`.
5. **D1 dry run.** Start the `collect` workflow by hand with `publish` off. It collects and uploads only the dry-run
   report and the encrypted evidence. Read which shops answer the GitHub runner before relying on any coverage number.
6. **Signing key (production).** On your own PC, not here:
   ```
   openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out rigmark-price-current.pem
   openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out rigmark-price-next.pem
   openssl pkey -in rigmark-price-current.pem -pubout -outform DER | base64 -w0
   openssl pkey -in rigmark-price-next.pem -pubout -outform DER | base64 -w0
   ```
   Store the **current** private key as the Actions secret `PRICE_SIGNING_KEY` (the whole PEM text) and its key id
   (for example `rigmark-2026a`, lower case, 3 to 40 characters) as the repository variable `PRICE_KEY_ID`. Keep an
   offline copy of both private keys. The **next** key stays offline until a rotation.
7. **App keys.** The app ships two public keys, current and next, in `PriceFile.TrustedKeys` (game-optimizer, phase E1),
   so the signing key can be rotated without a broken release: sign with "next", ship a release that trusts a new
   "next", then retire the old key.
8. **Publishing.** Record the standing authorization for the daily publication in the app's `SAFETY.md` (plan risk 7),
   then start `collect` with `publish` on. A schedule is phase D3 and is added only after that.

## Layout

```
sources/registry.json          allow-list (ships disabled)
sources/evidence-recipient.txt age public key for evidence (placeholder)
pricebot/                      the bot (stdlib Python)
conformance/PriceFileCheck/    .NET verifier used by the tests
parts/                         dataset copy the bot matches against (empty until step 2)
seed/                          hand-collected index seed, seed/<yyyy-mm>/<region>.csv
tests/                         unit tests and hand-written fixtures
.github/workflows/             tests (pull requests), collect (manual, guarded)
```
