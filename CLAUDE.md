<!-- pull-before-work: v1 -->
> **Pull before any edit.** First action after entering this repo, every session:
> ```bash
> git fetch --prune origin
> git pull --rebase origin "$(git branch --show-current)"
> git log --oneline HEAD..@{u} || true   # commits you don't have locally
> git log --oneline @{u}..HEAD || true   # commits not yet pushed
> ```
> If the branch has diverged or has no upstream, **stop and surface it** before editing — do not auto-merge, force-push, or cherry-pick blindly.

# Carvana Scraper — Agent Instructions

> **Personal project — NOT client work.** Evan's own used-car purchase research. No deploy
> pipeline, no hand-off requirements, no brand guidelines.

## What This Is
Ranks current Carvana inventory by price, mileage and vehicle history. Drives a dedicated headful
real-Chrome profile through four stages: read inventory out of Carvana's RSC payload → AutoCheck
report for every match → Carfax report for the top-N shortlist → disqualify, score, report.

Three front ends over **one** orchestration (`pipeline.execute`) — all thin callers, do not fork it:

- the **CLI**;
- a **local browser app** (`python3 -m carvana_scraper.app`) adding dropdowns, pasted-report
  ingest, and a local-Claude report reviewer;
- **GRID** (`web/`), a password-gated Vercel site that queues searches for other people. It holds
  the queue and results only — `carvana_scraper/worker.py` runs on each person's own laptop and
  does the browser work there, because a serverless host cannot.

**Read [`PROJECT_MAP.md`](PROJECT_MAP.md) first** — it is the source of truth for where things live,
the data flow through all three front ends, and a where-to-find table. Navigate by it instead of
grepping.

Then [`README.md`](README.md) for usage, and [`docs/RECON.md`](docs/RECON.md) before changing
anything that touches the sites — it records what was empirically verified on 2026-07-25, with
saved evidence in `fixtures/recon/`.

## Stack
- **Language:** Python 3.13 (system `python3`, no venv)
- **Dependencies:** Playwright only. Everything else is stdlib — no `pyyaml`, no `rich`, no `bs4`,
  and no web framework: the app is `http.server` plus one HTML page. Tests use stdlib `unittest`.
  **Keep it that way** unless there is a real reason. (This is also why pasted reports are text-only:
  a PDF would need a dependency, and an HTML paste would need a tag stripper.)
- **Browser:** Playwright + real Google Chrome (`channel="chrome"`), persistent profile in
  `.browser-profile/`, always headful.
- **Storage:** SQLite at `cache/carvana.db`; raw report text in `cache/raw/`; reports in `out/`.
- **`web/` is a separate deployable** with its own `package.json`: Next.js App Router + `pg`, no
  CSS framework, no ORM. The Playwright-only rule governs the Python package, not this. The
  worker's own networking is `urllib.request`, so the package itself stays dependency-free.
- **Deploy:** the CLI and local app run locally on demand. `web/` deploys to Vercel (root
  directory `web`) behind Deployment Protection; see [`web/README.md`](web/README.md).

## Non-negotiable invariants
These encode findings that cost real investigation. Do not "simplify" them away.

1. **Unknown ≠ clean.** History fields are tri-state (problem / explicitly clean / unknown). A
   vehicle with unknown decision fields never enters the main ranking. See `models.HistoryReport`.
2. **Both vendors, merged pessimistically.** AutoCheck was observed reporting "No Accidents" for a
   truck Carfax showed had been towed from an accident. If either vendor reports a problem, the
   vehicle has it; disagreements go in `conflicts` and get printed.
3. **`history_blocked` is never cached.** A DataDome puzzle is transient. Caching it would silently
   keep a car out of every future ranking. `cache.TTL_DAYS` deliberately omits that status.
4. **Listing prices are never cached.** Cars sell in days and Carvana drops prices.
5. **Challenge detection reads HTML, not visible text.** DataDome renders its puzzle in a
   cross-origin iframe, so `innerText` is empty. A text-only check silently misclassifies a
   solvable block as an unparseable page. See `browser.CHALLENGE_MARKERS`.
6. **Scores anchor to `--max-price` / `--max-miles`,** never to the observed range of the day's
   results, or an identical car would score differently run to run.
7. **The run manifest must reconcile.** Every stage count is reported and the process exits
   non-zero when they don't. Silent narrowing is the failure mode that matters here — a confident
   table of four cars when forty matched.
8. **Never auto-solve a challenge.** Detect, alert, wait for a human. No CAPTCHA services, no proxy
   rotation, no concurrency on the report stage.
9. **One orchestration.** `pipeline.execute` is the only copy of the four-stage flow; `cli.run`,
   the app, and `worker.run_one` are all thin callers. It implements invariant 7, so a second
   drifted copy would reintroduce exactly the failure that invariant catches. Every event's `text`
   is the exact string the CLI prints — changing one changes CLI output, which
   `tests/test_pipeline.py` pins.
10. **The reviewer never touches the ranking.** `app/review.py` reads report prose the scorer
    discards and returns commentary. Enforced in code, not the prompt: disqualified cars are absent
    from its input, findings naming an unknown VIN are dropped, any emitted score or ordering is
    discarded, and every quote is checked against the report — a live run produced 6 of 14 quotes
    that were paraphrased, so unverified ones are labelled rather than shown as evidence.
11. **A pasted report is not a shortcut past the rules.** Same parser, same pessimistic merge, same
    cache, same scorer. Refuse anything under 1,500 chars, anything matching a `CHALLENGE_MARKER`,
    and HTML/PDF — those parse to an all-unknown report instead of failing, which is worse than an
    error because the operator is told it worked. Always pass the VIN explicitly; both parsers
    self-extract one otherwise, so a mis-paste would be filed under the wrong car.
12. **`detect_challenge` is for report pages only.** On carvana.com it false-positives —
    `/cdn-cgi/challenge-platform/scripts/jsd/main.js` loads on every healthy `/cars` response.
    Validate the outcome instead; the extractors raise when content is absent. `docs/RECON.md` §(d1).
13. **No hardcoded zip.** `zip_code` defaults to `None` everywhere and resolves in
    `pipeline.execute` from `delivery.default_zip()` — this machine's captured location. It used to
    default to `"89002"`, which was fine for one operator and silently prices every other
    operator's search against the author's city. `None` means "no zip requested", and the
    mismatch warning is suppressed rather than fired on every run.
14. **The worker is scoped to its own machine.** Jobs belong to a worker token; the site password
    is shared by every visitor and so cannot identify a laptop. The worker token, the browser's
    owner key and the Vercel bypass secret are three different things — see `web/README.md`. Never
    let a route claim, write progress to, or complete a run that is not `worker_id`-matched.
15. **The web app renders `AppState.snapshot()`, it does not reshape it.** `app/serialize.py` is
    the single contract between the pipeline and *both* browser front ends. A field added there
    must land in `web/src/lib/types.ts` too. Report prose is the deliberate exception: it goes in
    `run_reports`, out of the result blob, because the run view polls every 2s.
16. **The delivery location is captured, never constructed.** Carvana honours `CVCurrentZip` +
    `CVCurrentCity` + `CVCurrentState` together and discards a partial triple, and the zip cookie is
    session-scoped so it must be replayed *before any navigation* on every session. City for an
    arbitrary zip is not ours to invent, so `--login` captures what Carvana wrote after the operator
    used its picker. `docs/RECON.md` §(a1) has the five observations. This **corrects** the earlier
    claim that the pricing zip could not be set — when a documented finding turns out to be wrong,
    reverify and rewrite it rather than working around it.

## Layout
| File | Purpose |
|------|---------|
| `carvana_scraper/cli.py` | argparse surface only. `run()` is a thin wrapper over `pipeline.execute` whose emit prints. |
| `carvana_scraper/pipeline.py` | **The** 4-stage orchestration, with `emit(event)` + `abort`. Shared by all three front ends. |
| `carvana_scraper/worker.py` | Claims queued jobs from the GRID site and runs them here. Third thin caller; reuses `AppState` verbatim. stdlib networking. |
| `carvana_scraper/app/` | Local browser UI. `server.py` routes, `runner.py` worker threads, `state.py` mutex-guarded snapshot, `serialize.py` dataclass→JSON, `ingest.py` pasted reports, `review.py` the Claude reviewer, `static/` the page. |
| `tools/extract_taxonomy.py` | Builds `config/carvana-taxonomy.json` from a saved or live `/cars` page. |
| `config/carvana-taxonomy.json` | Committed dropdown data: 40 makes → 528 models + inventory bounds. Derived from a **gitignored** fixture, so it must stay committed. |
| `carvana_scraper/browser.py` | Persistent real-Chrome session, human pacing, DataDome detection, manual assist, `--login`. |
| `carvana_scraper/rsc.py` | Extract vehicle records from Next.js RSC flight payload. Pure, testable. |
| `carvana_scraper/search.py` | Stage 1: page through results, filter locally, sniff the priced zip. |
| `carvana_scraper/delivery.py` | Capture + replay the delivery location that decides Carvana's pricing zip. |
| `carvana_scraper/vdp.py` | AutoCheck token from the wrapper page + cosmetic imperfections. |
| `carvana_scraper/history.py` | Stage 3 orchestration + the pessimistic merge. |
| `carvana_scraper/carfax.py` | Carfax text parser. Verdict column is authoritative, not the prose. |
| `carvana_scraper/autocheck.py` | AutoCheck text parser. Supplies the AutoCheck Score. |
| `carvana_scraper/scoring.py` | Disqualifiers, anchored weighted score, reasons. Pure. |
| `carvana_scraper/report.py` | Table, markdown, run manifest, exit code. |
| `carvana_scraper/models.py` | Shared dataclasses + the completeness contract. |
| `config/scoring.json` | Weights and disqualifier toggles. Tunable without code changes. |
| `docs/RECON.md` | Verified site behaviour + the evidence trail. Read before site-facing changes. |
| `.tmp/*.py` | Throwaway recon probes. Not part of the package; safe to delete. |

## Testing
```bash
python3 -m unittest discover -s tests -t . -v    # 255 tests, fully offline
```
Parser and ingest changes must be validated against `cache/raw/*.txt` (real archived reports) — those
tests skip automatically when the directory is empty, so run them locally where it isn't.

`tests/test_app_flow.py` drives the real HTTP server end to end with the browser stubbed, including
the paste path against an archived Carfax capture. No test invokes the real `claude` CLI.

**If you change `pipeline.execute`, prove CLI output did not move.** Restore the previous `cli.py`
from git as a baseline module, stub the I/O primitives, run both over a matrix of flag combinations
and diff stdout. That is how the extraction was verified byte-identical across 18 scenarios, and it
is far stronger than reading the diff.

Never test by hammering the live sites. Recon artifacts in `fixtures/recon/` exist so you don't
have to. When a live check is genuinely needed, `--search-only --limit 3 --max-pages 1` costs one
page load and no report fetches.

## Git
- **Remote:** `emb-marketing/carvana-scraper` (private)
- **Default branch:** `main` — personal solo project, following the `ioverlander-kml` precedent.
  No `dev` branch.
- Never commit `.browser-profile/`, `cache/`, or `out/`. Already gitignored — verify with
  `git diff --cached` before committing.
