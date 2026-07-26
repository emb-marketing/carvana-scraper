# PROJECT_MAP — carvana-scraper

**Last updated:** 2026-07-26

## What this is

A personal (not client) tool that answers one question on demand: of the cars currently listed on
Carvana matching given criteria, which is the best one to buy? It ranks by price and mileage weighed
against each vehicle's history reports, pulled from **two** vendors and merged pessimistically.

Three front ends sit over **one** orchestration: a CLI, a local browser app, and GRID — a
PIN-gated Vercel site anyone can use with nothing installed. The local app exists for the two things a
terminal does badly: dropdowns built from Carvana's own inventory taxonomy, and pasting in a report
the scraper was blocked from fetching.

The browser work always happens on someone's own machine, driving a dedicated headful Chrome
profile. GRID holds only the queue and the results; it cannot scrape, because the pipeline needs
real Chrome, a human for DataDome puzzles, minutes of wall clock, and a writable disk.

---

## Annotated tree

```
carvana_scraper/
  __main__.py       `python3 -m carvana_scraper`. sys.exit(main()) — the exit code is load-bearing.
  cli.py            argparse surface. run() is a 4-line wrapper over pipeline.execute.
  pipeline.py   ★   THE 4-stage orchestration. emit(event) + abort. Shared by all three front ends.
  browser.py        Persistent real-Chrome session, human pacing, challenge detection, --login.
  search.py         Stage 1: page through results, filter locally, sniff the priced zip.
  rsc.py            Pull vehicle records out of Next.js RSC flight payloads. Pure.
  vdp.py            AutoCheck token from the wrapper page + cosmetic imperfections.
  history.py    ★   Stage 3 fetch + the pessimistic merge + has_carfax().
  carfax.py         Carfax text parser. Pure str -> HistoryReport.
  autocheck.py      AutoCheck text parser. Pure str -> HistoryReport.
  scoring.py        Disqualifiers, anchored weighted score, reasons. Pure.
  report.py         Text table, markdown, RunManifest, exit code.
  models.py     ★   Listing / HistoryReport / ScoredVehicle + the completeness contract.
  cache.py          SQLite at cache/carvana.db, raw report text at cache/raw/.
  worker.py     ★   Claims jobs from GRID and runs them here. Third thin caller of the pipeline.

  app/              The local browser UI. Nothing here is imported by the CLI.
    __main__.py     `python3 -m carvana_scraper.app`
    server.py       ThreadingHTTPServer on 127.0.0.1:8765. Routes, validation, static files.
    runner.py       Worker threads: run, login, taxonomy refresh, review, paste.
    state.py        AppState — one lock, one snapshot. The only thing HTTP threads touch.
    serialize.py    Dataclasses -> JSON, injecting Listing's computed properties.
    ingest.py   ★   Pasted report -> parse -> merge -> cache -> rescore.
    review.py   ★   `claude -p` reviewer + its guardrails + quote verification.
    static/         index.html, app.css, app.js. One page, polls /api/state every second.

web/              GRID: the shared site. Separate deployable, own package.json. See web/README.md.
  schema.sql      workers / runs / run_reports. Apply once to Postgres.
  src/lib/        db.ts (pg tagged template), auth.ts (worker tokens), gate.ts (site PIN, edge-safe),
                  options.ts (clamping),
                  types.ts (mirrors AppState.snapshot() — keep in step with app/serialize.py).
  src/app/api/    gate, runs, runs/[id], runs/[id]/report, worker/{register,claim,progress,complete}
  src/app/link/   GET one-time link: binds this browser to the machine that printed it.
  src/app/setup/  Download + copy-paste command for running a worker locally.
  scripts/        copy-taxonomy.mjs, bundle-worker.mjs (builds the downloadable tarball)
  middleware.ts   Site PIN gate. Fails closed. /api/worker/* exempt (bearer-authenticated).
  src/app/        page.tsx (submit), gate/ (PIN), runs/[id]/ (live then final), globals.css
  src/components/ SearchForm, GridSlot, StartLights

tools/extract_taxonomy.py   Builds config/carvana-taxonomy.json from a saved or live /cars page.
config/scoring.json         Weights + disqualifier toggles. Tunable without code changes.
config/carvana-taxonomy.json  Committed dropdown data: 40 makes -> 527 models + bounds.
                              NOTE: makes have no `slug` — only models do.
docs/RECON.md               Verified site behaviour and the evidence trail. Read before site changes.
docs/SETUP.md               How to run the machine that serves the site.
tests/                      255 tests, all offline. See "Testing" below.
install.sh                  Public one-command install: curl … | bash. Downloads, installs, runs the app.
                            Reads /dev/tty, because under `curl | bash` stdin is the script itself.
run-app.command             Double-clickable launcher for the local app.
setup.command               Double-clickable first-run setup for the scraping machine.
.tmp/*.py                   Throwaway recon probes. Not package code; safe to delete.
```

★ = read these before changing behaviour. They hold the findings that cost real investigation.

---

## Entry points and data flow

**All three front ends call `pipeline.execute(options, emit, abort)`.** Do not fork it — it
implements the manifest reconciliation (invariant 7), and a drifted second copy would reintroduce
the exact failure that invariant catches.

```
CLI:  __main__ -> cli.main -> cli.run -> pipeline.execute(emit=print)
App:  app.__main__ -> server.serve -> POST /api/run -> runner.start_run
                                   -> worker thread -> pipeline.execute(emit=state.record_event)
                      GET /api/state <- state.snapshot() <- browser polls every 1s
GRID: worker.main -> Client.claim -> worker.run_one
                  -> pipeline.execute(emit=ProgressPusher -> state.record_event + POST progress)
                  -> POST complete { result: state.snapshot(), reports: [...] }
      browser polls GET /api/runs/[id] every 2s
```

**GRID's three secrets**, answering three different questions:

```
person  -> site PIN -> POST /api/gate -> HMAC cookie -> middleware lets pages + browser APIs through
may join -> GRID_ENROLL_SECRET -> x-grid-enroll on POST /api/worker/register only (fails closed)
machine -> .worker-token (0600) -> POST /api/worker/register -> Bearer on /api/worker/* (PIN-exempt)

claim a machine (optional):
  worker register -> one-time link_token -> operator opens GET /link?t=… -> grid_owner cookie

submit:  POST /api/runs         -> worker_id = this browser's machine, or NULL for the pool
claim:   POST /api/worker/claim -> own addressed jobs first, then unassigned;
                                   order by (worker_id is null), created_at; SKIP LOCKED
```

Nobody installs anything to *use* the site — unclaimed submissions run on whichever machine is
online. Claiming a machine is what makes your searches run on *your* laptop.

**The four stages**, in `pipeline.py`:

1. `search.collect_listings` — one page load per results page. Make/model via the URL path; year,
   price and mileage filtered locally in Python.
2. `history.get_or_fetch(want_carfax=False)` for **every** match — AutoCheck is Carvana-hosted and
   unmetered. Plus `vdp.fetch_imperfections`.
3. `provisional_rank` picks the top N, then `get_or_fetch(want_carfax=True)` for those only —
   Carfax is DataDome-gated at roughly 6 per session.
4. `scoring.score_all` -> `report.render` -> `report.write_markdown` -> exit code.

**The paste path bypasses the browser entirely**, which is why it works when a fetch cannot:

```
POST /api/ingest -> runner.apply_paste -> ingest.validate -> ingest.parse_paste
                 -> history.merge_reports([autocheck, carfax])   (AutoCheck first — order matters)
                 -> cache.put_history + cache.archive_raw
                 -> ingest.rescore -> report.render -> state.replace_scored
```

**The reviewer reads what the scorer discards:**

```
POST /api/review -> runner.start_review -> review.select_vehicles (top 5 rankable)
                 -> review.build_dossier (structured summary + cache/raw/*.txt in full)
                 -> `claude -p` on stdin, toolless, cwd outside the repo
                 -> validate_response (VIN allowlist, drop emitted rankings)
                 -> evidence_supported (locate every quote in the report)
```

---

## Where to find things

| Question | Look here |
|---|---|
| Add or change a CLI flag | `cli.py` `build_parser`, then `pipeline.RunOptions` |
| Change what a stage does | `pipeline.py` `_stage_one_search` / `_stage_two_autocheck` / `_stage_three_carfax` |
| Change the ranking maths | `scoring.py` + `config/scoring.json` |
| Change the text report | `report.py` `render` / `_table` / `RunManifest.lines` |
| A report field parses wrongly | `carfax.py` or `autocheck.py`; validate against `cache/raw/*.txt` |
| Two vendors disagree | `history.py` `merge_reports`, `_PROBLEM_FIELDS`, `_FIRST_WINS_FIELDS` |
| "Does this car have Carfax?" | `history.has_carfax` — the one predicate; do not re-inline it |
| Add a UI control | `app/static/index.html` + `app.js`, then `server.options_from_payload` |
| Add an API route | `server.Handler.do_GET` / `do_POST` route tables |
| Change what the browser sees | `app/state.py` `snapshot` + `app/serialize.py` — **and** `web/src/lib/types.ts` |
| Change the GRID site's look | `web/src/app/globals.css`; the brand lives in `layout.tsx` |
| Change what a submitted search may ask for | `web/src/lib/options.ts` `LIMITS` + `buildOptions` |
| Add a field to a queued job | `pipeline.RunOptions`, then `web/src/lib/options.ts` — the worker **rejects** unknown keys rather than ignoring them |
| GRID auth / pairing | `web/src/lib/auth.ts` and `web/README.md`'s table of the three secrets |
| A worker cannot reach the API | Missing `VERCEL_AUTOMATION_BYPASS_SECRET`; Deployment Protection gates API routes too |
| Deploy the site | `web/README.md` |
| Dropdown values are wrong/stale | `tools/extract_taxonomy.py`, or the app's Refresh taxonomy |
| A site-facing assumption | `docs/RECON.md` **before** touching `browser.py` / `search.py` / `rsc.py` |

---

## Conventions specific to this project

- **Dependencies: Playwright only.** Everything else is stdlib — no `bs4`, no `rich`, no `pyyaml`, no
  web framework. The app is `http.server` plus one HTML page, and `worker.py` uses
  `urllib.request`. This is why pasted reports are text-only: PDF needs a dependency and HTML needs
  a tag stripper. `web/` is a separate deployable with its own `package.json` and is not covered by
  this rule.
- **No hardcoded zip.** `zip_code` is `None` by default and resolves in `pipeline.execute` from
  `delivery.default_zip()` — this machine's captured location, not a constant.
- **Resolve module paths at call time, not as argument defaults.** `load_or_create_token` and
  `collect_reports` both read their module-level path inside the body. An argument default binds
  once at import, so a test patching the module attribute misses it — which is how the suite
  briefly wrote a real `.worker-token` into the repo.
- **Unknown is never clean.** History fields are tri-state. A vehicle missing any of the seven
  `DECISION_FIELDS` cannot be ranked. An AutoCheck-only car can *never* be complete, because
  `parse_autocheck_text` deliberately never sets `structural_damage` or `airbag_deployment` — that is
  the mechanism behind the NEEDS CARFAX bucket, not a bug.
- **`history_blocked` is never cached**; listing prices are never cached.
- **Scores anchor to `--max-price`/`--max-miles`**, never to the day's observed range, or an identical
  car would score differently run to run.
- **Never auto-solve a challenge.** Detect, alert, wait for a human.
- **`browser.detect_challenge` is for report pages only.** It false-positives on carvana.com; see
  `docs/RECON.md` §(d1). On a Carvana page, validate the extraction outcome instead.
- The full list is `CLAUDE.md` — 17 numbered invariants. Read them before changing behaviour.

---

## Testing

```bash
python3 -m unittest discover -s tests -t . -v      # 255 tests, fully offline
```

| File | Covers |
|---|---|
| `test_pipeline.py` (40) | The event contract, stage gating, completeness, warnings, abort, merge idempotency |
| `test_review.py` (45) | Dossier, guardrails, quote verification, invocation flags, failure modes |
| `test_ingest.py` (24) | Paste validation, vendor detection, merge order, rescore — against real archived reports |
| `test_app_flow.py` (23) | The real HTTP server end to end, browser stubbed, incl. the paste path |
| `test_scoring.py` (19) | Disqualifiers, completeness, cross-run score stability |
| `test_report.py` (17) | Manifest reconciliation, exit codes, bucketing, sort |
| `test_parsers.py` (15) | Both parsers, inline fixtures + real archives when present |
| `test_taxonomy.py` (14) | Extractor shape, slug/URL equivalence, failure modes |
| `test_worker.py` (28) | Token handling, bypass/bearer headers, unknown-key rejection, progress throttling, failure classification, report collection |

The web app has no automated tests; it was verified end to end against a local Postgres with the
real worker (see `web/README.md` for how to stand that up).

Tests that need real report text read `cache/raw/` and **skip when it is empty** — it is gitignored,
so a fresh clone has none.

**If you change `pipeline.execute`, prove CLI output did not move.** Restore the previous `cli.py`
from git as a baseline module, stub the I/O primitives, run both over a matrix of flag combinations
and diff stdout. That is how the extraction was verified byte-identical across 18 scenarios.

Never test by hammering the live sites. When a live check is genuinely needed,
`--search-only --limit 3 --max-pages 1` costs one page load and no report fetches.

---

## Known unverified paths

These need a human at the keyboard and are covered only by stubbed tests:

- A real DataDome puzzle during the Carfax stage — the app's challenge card, pause and resume, and
  GRID's "check your Chrome window" banner. The banner path was exercised with a synthetic
  `challenge` event, never a real puzzle.
- `--login` through the app (the Done button releasing the worker thread).
- **GRID against a real deployment.** The whole loop — register, pair, submit, claim, progress,
  complete, per-car prose — was verified against a local Postgres and `next dev` with the browser
  stubbed. Not yet verified: Vercel Deployment Protection plus the automation bypass secret, which
  is the one piece that cannot be tested locally and the most likely thing to be wrong first.
- `setup.command` on a machine that has never run this repo.

---

## Git

Remote `emb-marketing/carvana-scraper`, **public** since 2026-07-26 — `install.sh` at the root is the
one-command install, and it is what "somebody else can run this" means now. The GRID site's URL and
PIN remain unpublished: they exist only inside the gitignored `grid-worker.tar.gz` the site builds,
and must never be committed. **`main` only**, no `dev` branch — solo personal project, following the
`ioverlander-kml` precedent. Never commit `.browser-profile/`, `cache/` or `out/`; all three are
gitignored. `config/carvana-taxonomy.json` is derived from a gitignored fixture and **must** stay
committed, or a fresh clone has no dropdown data.
