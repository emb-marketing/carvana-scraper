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

Read [`README.md`](README.md) for usage and [`docs/RECON.md`](docs/RECON.md) before changing
anything that touches the sites — it records what was empirically verified on 2026-07-25, with
saved evidence in `fixtures/recon/`.

## Stack
- **Language:** Python 3.13 (system `python3`, no venv)
- **Dependencies:** Playwright only. Everything else is stdlib — no `pyyaml`, no `rich`, no `bs4`.
  Tests use stdlib `unittest`. **Keep it that way** unless there is a real reason.
- **Browser:** Playwright + real Google Chrome (`channel="chrome"`), persistent profile in
  `.browser-profile/`, always headful.
- **Storage:** SQLite at `cache/carvana.db`; raw report text in `cache/raw/`; reports in `out/`.
- **Deploy:** none — runs locally on demand.

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

## Layout
| File | Purpose |
|------|---------|
| `carvana_scraper/cli.py` | argparse surface + 4-stage orchestration. No interactive prompts (except `--login`). |
| `carvana_scraper/browser.py` | Persistent real-Chrome session, human pacing, DataDome detection, manual assist, `--login`. |
| `carvana_scraper/rsc.py` | Extract vehicle records from Next.js RSC flight payload. Pure, testable. |
| `carvana_scraper/search.py` | Stage 1: page through results, filter locally, sniff the priced zip. |
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
python3 -m unittest discover -s tests -t . -v    # 51 tests, fully offline
```
Parser changes must be validated against `cache/raw/*.txt` (real archived reports) — those tests
skip automatically when the directory is empty, so run them locally where it isn't.

Never test by hammering the live sites. Recon artifacts in `fixtures/recon/` exist so you don't
have to.

## Git
- **Remote:** `emb-marketing/carvana-scraper` (private)
- **Default branch:** `main` — personal solo project, following the `ioverlander-kml` precedent.
  No `dev` branch.
- Never commit `.browser-profile/`, `cache/`, or `out/`. Already gitignored — verify with
  `git diff --cached` before committing.
