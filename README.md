# Carvana Scraper

Personal tool that answers one question on demand: **of the cars currently on Carvana matching my
criteria, which is the best one to buy right now?** — weighing price and mileage against each
vehicle's history report.

Not client work. Runs locally, on demand, driven by a dedicated Chrome profile. There are two front
ends over one pipeline: a **local app** with dropdowns, and the **CLI**.

---

## Quick start — the app

```bash
cd ~/projects/carvana-scraper
./run-app.command          # or: python3 -m carvana_scraper.app
```

Opens a browser UI at **http://127.0.0.1:8765** — a fixed port, so it is bookmarkable; it falls back
to any free port if 8765 is taken, and `--port` overrides. Pick a make and model from dropdowns built
from Carvana's own inventory taxonomy (40 makes, 528 models), set your caps, and run. A separate
Chrome window opens — that is the scraper's own profile, and you need to see it to clear a DataDome
puzzle.

**Set both max price and max mileage.** They are the scoring anchors: without both, scores are
anchored to that run's own observed range and are not comparable with any other run. The UI warns
when either is blank.

**Check `Max reports` covers your matches.** It defaults to 40; a vehicle beyond it gets no history
and so appears in no section at all. The run now warns and fails reconciliation rather than
presenting a shorter table as complete, but raising the number is the actual fix.

What the app adds over the CLI:

- **Dropdowns instead of typed flags.** Make cascades to Model; year spans 2009–2027; price and
  mileage fields carry real inventory bounds. No more guessing whether `--model "Grand Highlander"`
  resolves to a real URL.
- **Paste a report the scraper could not fetch.** When Carfax is blocked, that car's card shows its
  report URL. Open it, select all, copy, paste it in — the text goes through the same parser, the
  same pessimistic merge, the same cache and the same scorer, and the car is rescored on the spot.
  This is the whole reason the app exists.
- **A local-Claude report reviewer.** Press *Review reports with Claude* and it reads the full report
  prose the scorer never sees, returning findings with quotes plus a pick. It cannot change the
  ranking (see below).

## Quick start — the CLI

```bash
# 1. One-time setup — opens Chrome, and you MUST set your delivery zip here (see below)
python3 -m carvana_scraper --login

# 2. A run. Criteria are per-run flags; there is no config file to edit.
python3 -m carvana_scraper \
    --make Toyota --model 4Runner \
    --year-min 2018 --year-max 2023 \
    --max-price 45000 --max-miles 80000 \
    --top-n 12
```

Requires Python 3.11+, Playwright (`pip install -r requirements.txt`), and real Google Chrome
installed. Everything else is standard library — the app adds no dependencies, using `http.server`
and one HTML page.

### The `--login` step is not optional

Two things persist and both matter:

- **Trust** — an aged profile with real cookies is most of why report fetches are not challenged
  immediately.
- **Your delivery ZIP.** Carvana decides the pricing zip from a cookie triple —
  `CVCurrentZip` + `CVCurrentCity` + `CVCurrentState`. A *partial* location is discarded, and the
  zip cookie is **session-scoped**, so it is absent at the start of every run and Carvana
  re-geolocates from your IP (observed: 89101, Las Vegas). That is why setting the picker once never
  used to stick.

  So `--login` now **captures** the location Carvana writes after you use its picker, and every
  later session **replays** it before the first request. Nothing is guessed — the city and state
  come from Carvana. Runs print `delivery location restored: 89002 (Henderson, NV)`, and the
  mismatch warning stops firing.

  Measured caveat: for Henderson vs Las Vegas the shipping cost is *identical* (same delivery
  market), so this changed no prices here. It matters if you search a distant car or move — and it
  stops a warning that otherwise fires on every single run, which is how real warnings get ignored.

  Stored at `.browser-profile/delivery-location.json`, gitignored with the profile.

---

## How it works

Four stages, shaped by what the sites actually permit (see [`docs/RECON.md`](docs/RECON.md) for the
evidence behind every claim here).

| Stage | What happens | Cost |
|---|---|---|
| 1. Search | Vehicle records are read out of Carvana's React Server Components payload — exactly what a normal page load delivers. No private API is called. | 1 request per results page |
| 2. AutoCheck | Every matching vehicle gets an Experian AutoCheck report, served from Carvana's own domain. | ~2 requests per vehicle, unmetered |
| 3. Carfax | Only the top-N shortlist. This is the rate-limited one. | 1 request per vehicle, ~6 per session |
| 4. Rank | Disqualify, score against fixed anchors, print and write the report. | free |

### Why both report vendors

Neither is a superset of the other. On a 2025 Tacoma, **Carfax reported a towed accident that
AutoCheck rendered as "No Accidents or Damage Reported."** AutoCheck alone would have scored that
truck clean and could have ranked it first.

So the two are **merged pessimistically**: if either vendor reports a problem, the vehicle carries
that problem, and any disagreement is printed rather than silently resolved.

AutoCheck contributes what Carfax lacks — an auction-issue check, an insurance total-loss check,
and the **AutoCheck Score** (Experian's 1-100 composite plus the band comparable vehicles fall in).
Carfax contributes the reliability forecast, estimated annual repair cost, and a materially better
accident record.

### The DataDome puzzle

Carfax sits behind DataDome. After roughly **6 report fetches in a session** it serves a
1,541-byte shell containing a puzzle in a cross-origin iframe. Measured behaviour:

- The tool detects it by HTML fingerprint (`captcha-delivery.com`, `datadome`) — a text-based
  check cannot see it, because the top document's visible text is empty.
- It then rings the terminal bell, prints a banner naming the vehicle, and **waits for you to
  solve the puzzle** in the Chrome window that is already open. It never attempts to solve one.
- **One solve restored access for 7+ consecutive fetches**, so a 12-car shortlist typically costs
  one or two puzzles.
- If nobody solves it within `--assist-timeout` (default 240s), the vehicle is recorded
  `history_blocked` — **never cached** — so the next run retries it.

Use `--unattended` to skip the pauses entirely and let blocked vehicles defer to a later run.

---

## Reading the output

Both the terminal and `out/carvana-<timestamp>.md` get the same text.

**The run manifest comes first, and it is the point.** The worst failure mode of a tool like this
is not crashing — it is printing a confident table of four cars when forty matched and thirty-six
were lost upstream. So every stage count is reported, `--limit` truncation is shown explicitly, and
**the process exits non-zero when the counts don't reconcile.**

Three sections follow:

- **RANKED** — vehicles with **both** reports. Only these are scored.
- **NEEDS CARFAX** — held out, split by remedy: *attempted but blocked* (re-run to retry) versus
  *outside the shortlist* (raise `--top-n`).
- **DISQUALIFIED** — branded title, odometer rollback, structural damage, airbag deployment, total
  loss. Listed with the reason, never scored.

Flags: `DQ` disqualified · `?` history not read · `!` needs manual review.

### Why a car with no Carfax is not ranked

Unknown is not the same as clean. Every history field is three-valued — problem / explicitly clean
/ unknown — and a vehicle with unknown decision fields never enters the ranking. AutoCheck alone
cannot establish structural damage or airbag deployment, so AutoCheck-only vehicles are always held
out.

### Why scores are comparable across runs

Price normalizes against `--max-price` and mileage against `--max-miles` — your own numbers, not
the observed range of that day's results. Min-max normalizing inside the result set would make an
identical car score 82 one day and 61 the next purely because the surrounding inventory changed.
Omit those flags and the tool falls back to observed maximums **and says so** in the manifest.

Missing signals are renormalized away rather than scored as zero, so a car lacking a KBB value
isn't penalized for the gap.

---

## Tuning the ranking

Everything is in [`config/scoring.json`](config/scoring.json) — no code change needed. Weights are
relative and get renormalized, so you can adjust one without rebalancing the rest.

```jsonc
"weights": { "price": 0.20, "mileage": 0.16, "kbb_delta": 0.15, "accidents": 0.13, ... }
"disqualifiers": { "title_brand_problem": true, "auction_problem": false, ... }
"expected_life_miles": 200000
```

`cost_per_remaining_mile` (the `$/MI` column) is `landed_price / (expected_life_miles - mileage)` —
a second, more legible view of value. It is displayed, not used as the primary sort. Change the
sort with `--sort score|price|cpm|mileage`.

---

## Caching

Deliberately split by how fast the data goes stale:

| Data | TTL | Why |
|---|---|---|
| Parsed history | 30 days | Report contents don't change; every avoided fetch is one less gated page load |
| `history_unavailable` | 7 days | May be a transient parse failure worth retrying |
| `history_blocked` | **never cached** | A puzzle is transient; caching it would silently keep a car out of future rankings |
| Listing price / mileage | **never cached** | Carvana drops prices and cars sell within days — a stale price would rank a car you can't buy |

Raw report text is archived to `cache/raw/` so parser work stays offline. `cache/` and `out/` are
gitignored.

---

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

51 tests, fully offline — no browser, no network. Fixtures are synthetic but copied from real
report layouts, so they run on a fresh clone; when `cache/raw/` has real archived reports, extra
tests validate against those too.

---

## Useful flags

| Flag | Effect |
|---|---|
| `--search-only` | List matching inventory and stop. No history fetches. |
| `--limit N` | Cap how many matches to evaluate. Truncation is reported in the manifest. |
| `--top-n N` | How many vehicles get a Carfax report (default 12). |
| `--max-pages N` | Search result pages to load (default 8, ~21 vehicles per page). |
| `--unattended` | Never pause for a puzzle; defer blocked reports to a later run. |
| `--no-carfax` | AutoCheck only. Nothing will enter the main ranking. |
| `--no-imperfections` | Skip Carvana's cosmetic-defect lookup. |
| `--sort` | `score` (default), `price`, `cpm`, `mileage`. |

The app exposes all of these as controls; `--debug` is omitted because it is parsed and never read.

---

## Pasting a report the scraper could not fetch

DataDome allows roughly six Carfax reports per session. Beyond that a vehicle is held out of the
ranking — under this tool's rules unknown never counts as clean, so a car with no Carfax cannot be
scored. Previously the only remedy was to re-run and hope. Now:

1. The app lists each held-out car with its report URL and what is still missing.
2. Open the report, select all (⌘A), copy (⌘C).
3. Paste it into that car's card.

The text takes the same path a scraped report does — `parse_carfax_text` → pessimistic merge with
the cached AutoCheck → `cache/` → rescore — so a pasted car is scored identically to a fetched one,
and it can just as easily land in **DISQUALIFIED**. It is archived to `cache/raw/` under the same
name the scraper uses, so it also feeds the reviewer and later runs.

**Visible text only.** Saved HTML and PDFs are refused, and deliberately so. Both parsers find
sections by whole-line equality (`line == "Structural Damage"`), so markup or PDF text layout
collapses that structure and yields a report whose every field is unknown. That would not raise — the
car would be correctly held out while you were told the paste worked. Text under 1,500 characters and
anything containing a challenge fingerprint are refused for the same reason: caching a DataDome shell
as history would drop the car from every future run.

---

## The Claude reviewer

`Review reports with Claude` shells out to your local `claude` CLI (`claude -p`, no API key) and has
one job: read the 6–15 KB of report prose per vendor that `scoring.py` never sees. The scorer reduces
each report to a handful of booleans and integers; the prose holds damage-severity wording,
rental/fleet/lease use, service gaps, ownership-length patterns, and the detail behind a vendor
disagreement.

**It cannot change the ranking, and that is enforced in code rather than asked for in the prompt:**

- The table renders from `ScoredVehicle` independently; any ordering or score the model emits is
  discarded, and the discard is shown rather than hidden. Invariant 6 holds.
- Disqualified vehicles are never in its input, so it cannot argue one back.
- A finding naming a VIN outside the reviewed set is dropped.
- **Every quote is checked against the report.** A live run returned 6 of 14 quotes that were
  paraphrased or stitched together; those are now labelled *not found in the report* in the UI. A
  claim you cannot check is not evidence.

Run it with `opus` (default) or `sonnet`. Never haiku. `codex` is a stub: the installed CLI on this
machine cannot launch its own binary (`ENOENT` on `codex-darwin-arm64`), so rather than ship an
untested backend it raises with the reinstall hint.

---

## Known limitations

- **`--zip` alone does not control Carvana's pricing zip** — but a captured location does. Run
  `--login` (or *Chrome login*) once and set the zip in Carvana's own picker; that location is saved
  and replayed on every later run, and the mismatch warning stops. Details below.
- **Coverage is bounded by `--max-pages`.** Roughly 21 vehicles per page. A broad search may hold
  more inventory than the run loads; the manifest reports what was actually seen.
- **Year / price / mileage filters are applied locally**, not by Carvana. Make and model go through
  the URL path. Carvana's private query-param vocabulary for the numeric filters is undocumented,
  and guessing it would be the most drift-prone thing here.
- **Parsers are calibrated on observed reports** — clean ones plus accident and open-recall cases.
  An unfamiliar verdict is recorded in `unrecognized_sections`, the field stays unknown, and the
  vehicle is flagged `!` for manual review. It is never silently scored as clean.
- **Owner counts can disagree** between vendors; both call it an estimate. The merge takes the
  higher and records the conflict.
- **Runs are attended by default** — that is the chosen trade for Carfax coverage.
- **The app holds one run in memory.** Listing prices are never cached (they change in days), so
  restarting the app means re-running the search. That is cheap: Carvana's search is not
  DataDome-gated and histories are cached 30 days, so a re-run mostly hits cache.
- **One browser session at a time.** `.browser-profile/` is single-instance; a second concurrent run
  is refused with the `rm -f` remediation rather than a traceback.
- **`browser.detect_challenge` must not be pointed at a Carvana page.** A normal `/cars` response
  loads Cloudflare's telemetry script at `/cdn-cgi/challenge-platform/scripts/jsd/main.js`, which
  matches the `challenge-platform` marker, so a healthy page reads as challenged. Validate the
  outcome instead — the extractors raise when the content is not there. See `docs/RECON.md` §(d1).
- **The taxonomy JSON goes stale.** It records inventory counts at extraction time; use *Refresh
  taxonomy* in the app, or re-run `python3 tools/extract_taxonomy.py`.

---

## Scope and volume

This does by automation, at human volume, exactly what you would do by hand for your own purchase:
open the free report Carvana already links. Accordingly: sequential fetches only, randomized
pacing, a hard `--max-reports` cap, a 30-day per-VIN cache so no report is fetched twice, and
**the tool never solves a challenge — a human does.**

Do not redistribute fetched report data. Do not add proxy rotation, CAPTCHA-solving services, or
concurrency to the report stage; each of those would turn human-scale research into something else.
