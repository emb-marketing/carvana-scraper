"""Background workers: the pipeline run, Chrome login, taxonomy refresh, review, and paste.

Everything that touches Playwright or sqlite runs on a thread that creates its own session and its
own connection, because both are thread-bound. The HTTP handler threads never do more than read a
snapshot or hand work to one of these.

Failures become UI states, not tracebacks. A locked browser profile, a changed RSC payload and a
missing scoring config are all things the operator can act on, so each gets a message and a hint.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

from .. import browser, pipeline, report as report_mod, rsc, scoring
from ..cache import connect
from ..pipeline import RunOptions
from . import ingest as ingest_mod, review as review_mod
from .serialize import has_carfax
from .state import AppState

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TAXONOMY_PATH = PROJECT_ROOT / "config" / "carvana-taxonomy.json"


def _spawn(target, *args, name: str = "worker") -> threading.Thread:
    """Start a daemon thread so a hung browser can never block interpreter exit."""
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


# ---- the pipeline run --------------------------------------------------------------------


def start_run(state: AppState, options: RunOptions) -> None:
    """Run the pipeline on a worker thread, streaming events into `state`."""
    abort = state.begin_run(options)
    _spawn(_run_worker, state, options, abort, name="pipeline")


def _run_worker(state: AppState, options: RunOptions, abort: threading.Event) -> None:
    try:
        result = pipeline.execute(options, emit=state.record_event, abort=abort)
        state.finish_run(result)
    except browser.ProfileLockedError as exc:
        # The dedicated profile is single-instance. The exception text already carries the exact
        # `rm -f` remediation, so it is passed through rather than paraphrased.
        state.fail_run("profile_locked", str(exc),
                       "Close the other Chrome window using .browser-profile, or run the rm -f "
                       "command above, then try again.")
    except rsc.PayloadShapeError as exc:
        state.fail_run("payload_shape", str(exc),
                       "Carvana's page structure changed. Re-run the discovery in docs/RECON.md "
                       "before trusting any result.")
    except FileNotFoundError as exc:
        state.fail_run("missing_config", str(exc),
                       "config/scoring.json is required — it holds the weights and disqualifiers.")
    except Exception as exc:  # noqa: BLE001 - a worker thread must never die silently
        state.fail_run(type(exc).__name__, str(exc), traceback.format_exc(limit=6))


def _recount_carfax(result, pasted_vins: set[str]) -> None:
    """Recompute the Carfax counters after a paste changed the histories.

    A paste counts as an attempt that succeeded, so it widens the population beyond the Carfax
    shortlist. Without that, pasting a report during a --no-carfax run (where the shortlist is
    empty) would leave the manifest reporting "Carfax parsed: 0" for a car whose Carfax it holds.

    Derived from a set of VINs rather than incremented, so pasting the same report twice cannot
    inflate anything.
    """
    considered = set(result.shortlist_vins) | set(pasted_vins)
    parsed = sum(1 for vin in considered if has_carfax(result.histories.get(vin)))
    result.manifest.carfax_attempted = max(result.manifest.carfax_attempted, len(considered))
    result.manifest.carfax_parsed = parsed
    result.manifest.carfax_blocked = max(0, result.manifest.carfax_attempted - parsed)


# ---- Chrome login ------------------------------------------------------------------------


def start_login(state: AppState, done: threading.Event) -> None:
    """Open the profile's Chrome on Carvana and hold it until `done` is set.

    `browser.login()` is not reused: it ends in a bare `input()`, and reading a stdin that is not a
    TTY raises EOFError, which would take the server down. The GUI's Done button sets `done`
    instead.
    """
    state.begin_login()
    _spawn(_login_worker, state, done, name="login")


def _login_worker(state: AppState, done: threading.Event) -> None:
    try:
        with browser.session() as context:
            cookies = browser.open_login_page(context)
            state.note("login", f"Chrome is open. Cookies currently in profile: {cookies}")
            state.note("login", "1. " + browser.LOGIN_INSTRUCTIONS[0])
            state.note("login", "2. " + browser.LOGIN_INSTRUCTIONS[1])
            # No timeout: the operator may take as long as they need with the location picker.
            done.wait()
            state.note("login", f"Profile saved. Cookies after session: {len(context.cookies())}")
    except browser.ProfileLockedError as exc:
        state.fail_run("profile_locked", str(exc), "Close the other Chrome window and try again.")
        return
    except Exception as exc:  # noqa: BLE001
        state.fail_run(type(exc).__name__, str(exc), traceback.format_exc(limit=6))
        return
    state.finish_login()


# ---- taxonomy refresh -------------------------------------------------------------------


def start_taxonomy_refresh(state: AppState) -> None:
    """Re-fetch /cars through the real browser and rewrite the taxonomy JSON."""
    state.begin_login()  # same exclusivity as login: one browser session at a time
    _spawn(_taxonomy_worker, state, name="taxonomy")


def _taxonomy_worker(state: AppState) -> None:
    try:
        # Imported here, not at module scope: tools/ is a scripts directory, not part of the
        # package, so the app must not fail to start when it is absent.
        import sys
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from tools.extract_taxonomy import extract_taxonomy, write_taxonomy

        state.note("taxonomy", "Loading carvana.com/cars to re-read the filter taxonomy…")
        with browser.session() as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.carvana.com/cars", wait_until="domcontentloaded",
                      timeout=90_000)
            browser.human_pause(3.0, 6.0)
            challenge = browser.detect_challenge(page)
            if challenge:
                state.fail_run("challenge", f"carvana.com served a challenge: {challenge[0]}",
                               "Solve it in the Chrome window, then refresh the taxonomy again.")
                return
            html = page.content()

        document = extract_taxonomy(html, source="https://www.carvana.com/cars")
        path = write_taxonomy(document, TAXONOMY_PATH)
        models = sum(len(make["models"]) for make in document["makes"])
        state.note("taxonomy",
                   f"Taxonomy refreshed: {len(document['makes'])} makes, {models} models "
                   f"-> {path.name}")
    except Exception as exc:  # noqa: BLE001
        state.fail_run(type(exc).__name__, f"taxonomy refresh failed: {exc}",
                       "The committed config/carvana-taxonomy.json is unchanged.")
        return
    state.finish_login()


# ---- Claude review ----------------------------------------------------------------------


def start_review(state: AppState, backend: str, model: str, count: int) -> None:
    """Review the top N rankable vehicles' report text on a worker thread."""
    state.set_review_running(True)
    _spawn(_review_worker, state, backend, model, count, name="review")


def _review_worker(state: AppState, backend: str, model: str, count: int) -> None:
    try:
        result = state.result
        if result is None:
            raise review_mod.ReviewError("no completed run to review")

        vehicles = review_mod.select_vehicles(
            result.scored, report_mod._sort_key(result.options.sort), count)
        if not vehicles:
            raise review_mod.ReviewError(
                "no vehicle has both reports yet, so there is nothing to read. "
                "Paste a Carfax report for one of the held-out vehicles first.")

        state.note("review", f"Reading {len(vehicles)} vehicle(s) of report text via {backend} "
                             f"({model})… this takes a minute.")
        review = review_mod.run_review(vehicles, backend=backend, model=model)
        state.set_review(review)
        state.note("review", f"Review complete: pick {review.get('pick_vin') or '(none)'}, "
                             f"{len(review['findings'])} finding(s).")
    except review_mod.ReviewError as exc:
        # Never fatal: the deterministic ranking stands on its own.
        state.set_review({"error": str(exc)})
        state.note("review", f"Review unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001
        state.set_review({"error": f"{type(exc).__name__}: {exc}"})
        state.note("review", f"Review failed: {type(exc).__name__}: {exc}")


# ---- pasted report ----------------------------------------------------------------------


def apply_paste(state: AppState, vin: str, text: str, vendor: str | None = None) -> dict[str, Any]:
    """Ingest a pasted report, then rescore and re-render the run.

    Runs synchronously on the calling request thread: no browser is involved and parsing is fast,
    so the operator gets the verdict in the same response that submitted the paste.

    Raises:
        ingest_mod.IngestError: If the paste is not usable, or the VIN is not in this run.
    """
    result = state.result
    if result is None:
        raise ingest_mod.IngestError("no completed run to attach this report to.",
                                     hint="Run a search first.")

    vin = vin.strip().upper()
    listing = state.listing_for(vin)
    if listing is None:
        raise ingest_mod.IngestError(f"{vin} is not one of this run's vehicles.",
                                     hint="Paste the report from the card for that specific car.")

    connection = connect()  # this request thread's own connection; sqlite3 is thread-bound
    try:
        summary = ingest_mod.ingest(text, listing, state.history_for(vin), connection, vendor)
    finally:
        connection.close()

    merged = summary.pop("report")
    result.histories[vin] = merged

    config = result.config or scoring.load_config()
    scored, anchor_info = ingest_mod.rescore(result, config)
    result.scored = scored
    result.anchor_info = anchor_info
    _recount_carfax(result, state.pasted_vins | {vin})

    # Reusing the same manifest is safe: render() assigns ranked/needs_carfax/disqualified/
    # conflicts rather than incrementing them, so re-rendering is idempotent.
    body = report_mod.render(scored, result.criteria, result.manifest,
                             sort_by=result.options.sort)
    result.exit_code = report_mod.manifest_exit_code(result.manifest)
    state.replace_scored(scored, body, result.manifest)

    rankable = next((v for v in scored if v.listing.vin == vin and v.is_rankable), None)
    summary["now_ranked"] = rankable is not None
    summary["score"] = rankable.score if rankable else None
    summary["disqualified"] = any(
        v.listing.vin == vin and v.is_disqualified for v in scored)
    summary["disqualifiers"] = next(
        (list(v.disqualifiers) for v in scored if v.listing.vin == vin), [])

    state.record_ingest({key: value for key, value in summary.items() if key != "report"})
    state.note("ingest", _paste_note(summary))
    return summary


def _paste_note(summary: dict[str, Any]) -> str:
    """One-line outcome for the event feed."""
    label = summary["label"]
    if summary["disqualified"]:
        return (f"Pasted {summary['vendor']} for {label}: DISQUALIFIED "
                f"({', '.join(summary['disqualifiers'])})")
    if summary["now_ranked"]:
        return f"Pasted {summary['vendor']} for {label}: now ranked, score {summary['score']}"
    missing = ", ".join(summary["missing_decision_fields"]) or "none"
    return (f"Pasted {summary['vendor']} for {label}: still held out "
            f"(missing: {missing})")
