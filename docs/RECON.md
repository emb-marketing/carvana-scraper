# Carvana Recon — reverse-engineered endpoints and page structure

**Run date:** 2026-07-25 · **Method:** headful real Chrome, dedicated profile, human-paced.
**Volume:** 2 search pages, 2 vehicle detail pages, 1 AutoCheck page, 1 Carfax report.
**Evidence:** `fixtures/recon/` (payloads, HTML, screenshots, `manifest.json`, `probe-findings.json`).

> **Headline: the plan's core premise did not hold, in our favour.** The Carfax report Carvana
> links is a free partner report that rendered **completely unchallenged** on the first fetch from
> a warm real profile. And Carvana's search results page already embeds every vehicle record we
> need, so no JSON API has to be reverse-engineered.

---

## (a) Search — no usable JSON API; the SRP HTML carries the records

**Negative result:** every candidate search endpoint failed with `TypeError: Failed to fetch`
(CORS / non-existent) when POSTed from an authenticated `www.carvana.com` document:

```
apik.carvana.io/merch/search/api/{v1,v2}/search
apik.carvana.io/merch/search/api/v1/{vehicles,srp,inventory}
```

The SRP is a Next.js **App Router / RSC** page (`__NEXT_DATA__` absent; 55 × `self.__next_f.push`).
The vehicle list arrives server-rendered inside the RSC flight payload, which is why no client XHR
carries it.

**Positive result — verified extraction.** Concatenating the flight chunks and brace-matching
objects that contain `"vin"` recovers **21 of 21 complete vehicle records** from
`fixtures/recon/search-filtered.html`:

```python
chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', html)
stream  = "".join(json.loads(c) for c in chunks)   # 800,284 bytes decoded
# then brace-match outward from each '"vin":' to recover whole JSON objects
```

Each record carries 59 keys. The ones that matter:

| Field | Example | Use |
|---|---|---|
| `vin` | `JTEEU5JR6P5298535` | Carfax URL key, cache key |
| `vehicleId` | `4568548` | VDP / inspection / AutoCheck key |
| `year` `make` `model` `trim` | 2023 Toyota 4Runner SR5 | display + filter |
| `mileage` | `29677` | scoring |
| `price.total` | `38990` | scoring |
| `price.transportCost` (also top-level `transportCost`) | `0` / `1990` | landed price |
| `price.kbbValue` | `38652` | **price-vs-market delta** |
| `price.msrp`, `price.marketAdjustment` | `40155`, `-1165` | depreciation context |
| `stockNumber`, `vdpSlug` | `2005072229`, `2023-toyota-4runner-sr5` | links |
| `vehicleTags[]` | `RecentlyAdded` | freshness signal |

**Filter contract.** The sibling `POST /merch/search/api/v1/suggest/filters` call reveals the
envelope the SRP uses:

```json
{"browserCookieId": "...", "dealershipId": null,
 "filters": {"makes": [{"name": "Toyota", "parentModels": [{"name": "4Runner"}]}]},
 "pagination": {"page": 1, "pageSize": 24},
 "requestedFeatures": ["ExcludeFacetData","HideImpossibleCombos","LocationBasedPrefiltering","ApplyTradeIn"],
 "sortBy": "MostPopular", "zip5": "89101"}
```

Make/model map to the **URL path** (`/cars/toyota-4runner`) and that is confirmed working. The
query-param names for year / price / mileage were **not** determined.

**Recommendation:** don't guess them. Filter make/model server-side via the path (confirmed
stable), then apply year / price / mileage **client-side in Python** over the extracted records,
bounded by `--max-pages`. Guessing Carvana's private param vocabulary is the single most
drift-prone thing we could build; arithmetic on records we already hold cannot silently break.

**Zip matters:** with no location set, Carvana defaulted to `zip5=89101` (Las Vegas), which drives
`transportCost` and delivery estimates. `--zip` must be passed and verified in the payload.

### (a1) The pricing zip IS controllable — a complete cookie triple, replayed every session

**This corrects an earlier conclusion in this document.** The first pass recorded that `--zip`
could not influence Carvana's pricing zip. Wrong, and wrong in a way worth writing down. Five
observations, 2026-07-25, each a fresh session reading `serverZip` out of the SRP and sniffing the
`apik.carvana.io` request:

| cookies set before the first request | Carvana priced with |
|---|---|
| none | 89101 (IP) |
| `CVCurrentZip` only | 89101 |
| `CVCurrentZip` + `CVCurrentSource` | 89101 |
| `CVCurrentZip` + `CVCurrentSource` + `CVCurrentAccuracyRadius` | 89101 |
| **`CVCurrentZip` + `CVCurrentCity` + `CVCurrentState`** | **89002, as asked** |
| the same three, asking `84604` (control) | **84604, as asked** |

Two facts explain the original mistake:

1. **A partial location is discarded.** Carvana wants zip, city and state together; anything less
   and it re-geolocates. `CVCurrentSource` and `CVCurrentAccuracyRadius` are irrelevant — the
   working run had `Source` rewritten from `user` to `unknown` mid-load and still priced correctly.
2. **`CVCurrentZip` is session-scoped.** It is absent at the start of every new browser session, so
   Carvana re-derives it from the IP each run. A location set once in the picker therefore never
   survived to the next run, which reads exactly like "cannot be forced".

Carvana also rewrites the family back to its IP guess *during* the load — after the pricing request
has already gone out with the value we set. So the cookies afterwards are not evidence of what was
used; the sniffed request and `serverZip` are.

Because the city for an arbitrary zip is not something to invent, the implementation **captures**
rather than constructs: `--login` reads the triple Carvana itself wrote once the operator has used
the picker, saves it to `.browser-profile/delivery-location.json`, and `browser.session()` replays
it before any navigation. See `carvana_scraper/delivery.py`.

Measured impact for the operator's own case: **none.** 89002 (Henderson) and 89101 (Las Vegas) are
the same Carvana delivery market — across six shared vehicles the shipping cost was identical, five
of them free and one $390 either way. The fix is worth having for correctness and because a warning
that fires on every run is a warning nobody reads, not because it changes prices here.

---

## (b) VDP fields — pricing is on the SRP record; history is not

Already on the SRP record (no per-vehicle fetch needed): VIN, price, shipping/transport cost,
KBB value, MSRP, market adjustment, mileage, year/make/model/trim, stock number, tags.

**Not** on the SRP record: owner count, accident history, title status, service records — these
come from Carfax only.

Extra Carvana-hosted detail, if ever wanted (both plain JSON on `apik.carvana.io`, no protection):
- `GET /merch/vehicledetails/api/v1/inspection/report/<vehicleId>` — 42 KB, Carvana's own
  inspection with `inspectonCategories` (sic).
- `GET spinnerdata.carvana.io/spinnerdata/<stockNumber>/spinnerData.json` — includes
  `imperfections`, `features`.

VDPs are RSC too (`had_next_data=False`), and the VIN is not embedded as `"vin":"…"` — but it is
trivially recoverable from the Carfax anchor's `vin=` parameter.

---

## (c) Report link — **CASE 1 confirmed: VIN-keyed off-site URL, not tokenized**

The hypothesis was exactly right. Both VDPs carried, with `target="_blank"`:

```
https://www.carfax.com/VehicleHistory/p/Report.cfx?partner=CVN_0&vin=<VIN>
```

Anchor text `View report` / `View Report (opens in a new tab)`. **No token, no expiry, no
session coupling** — the URL is fully reconstructible from the VIN alone, so it can be opened in a
fresh tab later. No in-session click required, and no VDP visit is strictly needed to obtain it.

**Also found (not pursued):** a Carvana-hosted AutoCheck report at
`/vehicle/autocheck/<vehicleId>`, whose content is an iframe from
`apik.carvana.io/merch/merchui/api/v1/autocheck?vehicleId=<id>&token=<token>` (token is in the
page HTML). Its content was **not** verified — the probe's frame matcher matched the parent
document instead of the iframe. Dropped deliberately: Carfax is the report that was asked for, it
works unchallenged, and AutoCheck would be a redundant second source. Documented here in case
Carfax ever starts blocking — this is the fallback to build, and it lives on Carvana's own domain.

---

## (d) Protection — **DataDome, triggering after ~6 Carfax reports per session**

> The first pass concluded "no protection observed." That was wrong, and it was wrong because
> n = 1. A 10-vehicle sample settled it.

**Result of the 10-vehicle run:** fetches #1–#6 returned real reports (6.4–14.8 KB of text);
fetches #7–#10 returned an identical **1,541-byte shell** every time.

The blocker is **DataDome**, not Cloudflare:

```html
<title>carfax.com</title>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=…&hash=…&cid=…">
<script src="https://ct.captcha-delivery.com/c.js">
```

**Two things this got wrong on the first pass, both worth recording:**

1. **The challenge is a solvable puzzle, and it was visibly appearing** — the operator watched
   DataDome slider puzzles pop up in the Chrome window. An earlier note here claimed "no
   challenge UI to solve"; that was an artifact of bad detection, not the truth.
2. **Text-only challenge detection cannot see it.** DataDome renders the puzzle in a cross-origin
   iframe, so the top document's `innerText` is empty and its HTML is tiny. The probe logged
   `challenged: 0` for four pages that were plainly challenged. A detector keying on visible text
   therefore misclassifies a *solvable, retryable* block as an *unparseable* page — and
   `history_unavailable` gets cached for 7 days while `history_blocked` correctly does not.

**Detection now used** (`browser.CHALLENGE_MARKERS`), verified against all 10 saved pages —
4 blocked / 6 real, zero false positives:
- HTML fingerprints `captcha-delivery.com`, `datadome`, `geo.captcha-delivery`
- Cloudflare / Imperva / Akamai / PerimeterX / hCaptcha markers retained for vendor changes
- A structural backstop: HTML < 6 KB *and* text < 250 B is reported as a challenge, so a new
  vendor degrades to "blocked, retry later" rather than "unavailable, cache it"

**Operational consequence:** Carfax supports roughly **6 reports per session** unattended. Beyond
that a human solves a puzzle (which sets a `datadome` cookie in the dedicated profile, allowing
the run to continue) or the remaining VINs are deferred to a later run.

### (d1) `detect_challenge` is for report pages only — it false-positives on carvana.com

Verified 2026-07-25 while adding the app's taxonomy refresh. **`browser.detect_challenge` must not
be pointed at a Carvana page.** A perfectly normal `/cars` response loads Cloudflare's bot-telemetry
script:

```html
<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js">
```

That path contains `challenge-platform`, which is one of `CHALLENGE_MARKERS`, so a healthy page is
reported as challenged. Confirmed against the known-good saved page
`fixtures/recon/search-base.html` (2.1 MB, 21 vehicle records recovered from it): the marker is
present, and it is the *only* marker present.

The distinction is `/cdn-cgi/challenge-platform/scripts/jsd/…` (telemetry, always loaded, harmless)
versus a real Cloudflare interstitial. **The marker list is not changed** — no real Cloudflare
challenge has ever been captured from these sites to verify a narrower pattern against, DataDome is
the actual blocker on Carfax, and guessing at anti-bot signatures is the drift this document exists
to prevent.

What to do instead, on a Carvana page: **validate the outcome, not the page.** The extractors are
self-validating — `rsc.extract_vehicle_records` raises `PayloadShapeError` and
`tools.extract_taxonomy.extract_taxonomy` raises `TaxonomyShapeError` when the content is not there,
so a challenge page, an error page and a layout change all fail loudly and none of them can be
mistaken for real data. This is why `search.collect_listings` never calls `detect_challenge` and is
unaffected.

Scope of the false positive, checked: `detect_challenge` is called only from
`history._fetch_report_page`, i.e. on `carfax.com` and the AutoCheck report host. A live run on
2026-07-25 fetched and parsed 3/3 AutoCheck reports, so neither report host carries this script and
the scraper's own paths are correct.

---

## (d2) AutoCheck — **unlimited, unchallenged, and on Carvana's own domain**

Because Carfax rate-limits, the Carvana-hosted AutoCheck report was re-tested properly. (The
first attempt failed for a trivial reason: the wrapper page's own URL contains "autocheck", so the
frame matcher selected the parent document instead of the iframe.)

**Result: 10/10 usable, every one HTTP 200, no CAPTCHA — including all four vehicles where Carfax
was blocked.** 7.7–12.3 KB of report text each.

```
wrapper : https://www.carvana.com/vehicle/autocheck/<vehicleId>
report  : https://apik.carvana.io/merch/merchui/api/v1/autocheck?vehicleId=<id>&token=<token>
```

The token sits in the wrapper page's HTML, so the flow is: load wrapper (cheap, unlimited),
regex the iframe `src`, navigate to it.

AutoCheck's `Vehicle History at a Glance` covers every planned disqualifier and adds signals
Carfax does not carry:

| AutoCheck field | Example |
|---|---|
| `State Title Brand` | `Clean` |
| `Auction Brand / Issues` | `No Issue` |
| `Accident / Damage` | `No Accidents or Damage Reported` |
| `Insurance Loss / Transfer` | `No Issue` |
| `Odometer Check` | `Last reported odometer: 54,817 (02/24/2025)` |
| `Open Recall Check` | `No Open Recalls` |
| `Service / Repair` | `10 Service Record(s) Reported` |
| `Vehicle Usage` | `Lease` |
| **`AutoCheck Score`** | **`96`, with peer range `76`–`86`** |

The AutoCheck Score with its similar-vehicle range is the single best normalized quality signal
found anywhere in this recon: Experian's own composite, plus the band comparable cars fall in.

---

## (d3) **The two vendors disagree — so both are required**

On the 2025 Toyota Tacoma (`3TYKD5HN3ST028017`):

| Source | Accident finding |
|---|---|
| **Carfax** | `ACCIDENT` — reported 10/17/2025, *minor to moderate damage*, **vehicle towed**, airbags did not deploy |
| **AutoCheck** | `Accident / Damage → No Accidents or Damage Reported` |

**AutoCheck missed a real, towed accident that Carfax caught.** This is the finding that settles
the architecture: AutoCheck alone would have scored that truck as clean and could have ranked it
first. Neither source is a superset of the other.

**Therefore the two are merged pessimistically:** if *either* vendor reports a problem, the
vehicle carries that problem. Where they disagree, the disagreement itself is surfaced in the
report rather than silently resolved.

---

## (e) Vendor — Carfax, free via `partner=CVN_0`

The report is the full consumer Carfax (the page even shows the $49.99 retail price alongside the
free partner view). Structure is clean and stable — every planned disqualifier has its own
labelled section:

```
NO ACCIDENTS REPORTED / No Accidents or Damage Reported to CARFAX
13 Service History Records
2 Previous Owners
Types of Owners: Personal Lease, Personal
Last Owned in Montana
23 Detailed Records Available
Total Loss              → No total loss reported to CARFAX.
Structural Damage       → No structural damage reported to CARFAX.
Airbag Deployment       → No airbag deployment reported to CARFAX.
Odometer Check          → No indication of an odometer rollback.
Accident / Damage       → No accidents or damage reported to CARFAX.
Manufacturer Recall     → No open recalls reported to CARFAX.
Salvage | Junk | Rebuilt | Fire | Flood | Hail | Lemon   (title-brand check)
Odometer Brands
Owner 1 / Owner 2 columns → "No Issues Reported" per owner per category
```

**Bonus signals not in the original plan**, and genuinely useful for ranking:
- `3-Year Reliability Forecast` — Fair / Good / Great, model+region+mileage cohort
- `Average Repair Cost` — e.g. `$320 avg per year`
- `Top 25% compared to similar vehicles`

Parsing target is `document.body.innerText`, not the DOM — the text layout above is stable and far
less brittle than class-name selectors.

---

## Consequences for the implementation plan

1. **Step 4 (search)** — parse the RSC flight payload from the SRP HTML. No API replay. Make/model
   via URL path; year/price/mileage filtered client-side.
2. **Step 5 (vdp)** — still needed, but cheap and unprotected: the wrapper page yields the
   AutoCheck token, and Carvana's imperfection/inspection JSON lives on the same domain. All
   `carvana.com` traffic is unmetered as far as this recon can tell.
3. **Step 6 (history) — two sources, merged pessimistically.**
   - **AutoCheck for every vehicle.** Unlimited, unchallenged, carries all disqualifiers plus the
     AutoCheck Score. This is the baseline that guarantees every car gets *some* history.
   - **Carfax for as many as the session allows** (~6 before DataDome), with manual-assist to
     continue and `history_blocked` + resume for the rest. Carfax is not optional garnish — it
     caught a towed accident AutoCheck missed.
   - Merge rule: any problem reported by either vendor counts; disagreements are surfaced.
4. **Step 7 (scoring)** — considerably richer than planned: price-vs-KBB delta, **AutoCheck Score
   vs its peer range**, Carfax reliability forecast, average annual repair cost, plus landed
   price, mileage, owners, accidents, title brands, auction/insurance-loss flags and Carvana
   imperfections.
5. **Resumability is now load-bearing, not a nicety.** Because Carfax yields ~6 reports per
   session, a 40-vehicle run cannot complete its Carfax pass in one go unattended. The run must
   fetch until blocked, persist what it got, report exactly what is missing, and let a later run
   continue. `history_blocked` is never cached, precisely so it retries.
6. **Still true and still load-bearing:** the split cache (history 30 days / prices never), the
   `data_completeness` rule (a blocked vehicle is never scored as clean), absolute-anchor scoring,
   and the run manifest with a non-zero exit when stage counts fail to reconcile.

## Verified artifacts backing this document

**Committed** (small, no third-party report content):

| Evidence | Path |
|---|---|
| Captured JSON endpoint catalog + request bodies (session ids redacted) | `fixtures/recon/manifest.json` |
| 10-vehicle Carfax sample — 6 real / 4 DataDome | `fixtures/recon/report-sample.json` |
| 10-vehicle AutoCheck sample — 10/10 usable | `fixtures/recon/autocheck-sample.json` |
| Puzzle test — 1 solve restored 7+ consecutive fetches | `fixtures/recon/puzzle-test.json` |
| Search-endpoint probe + AutoCheck iframe discovery | `fixtures/recon/probe-findings.json` |
| Vehicle record shape (59 keys, one sample) | `fixtures/recon/contracts/vehicle-record-shape.json` |
| Search request envelope / filter contract | `fixtures/recon/contracts/search-request-contract.json` |
| Imperfections payload shape | `fixtures/recon/contracts/imperfections-shape.json` |

**Deliberately not committed** — gitignored, regenerate with the `.tmp/` probes if needed:

- Carfax and AutoCheck report HTML / text / screenshots. These are licensed third-party report
  content; they live in `cache/raw/` locally and are never redistributed. The parser tests read
  them when present and skip when absent.
- Search and detail page dumps (2 MB+ each) and full-page screenshots.
- Third-party analytics and asset-manifest payloads captured incidentally during recon — noise.
