"""The four-stage ranking run, with progress delivered as events instead of prints.

This is the single orchestration shared by both front ends. `cli` wraps it with an emit callback
that prints; the local app wraps it with one that appends to a state object the browser polls.
Keeping one copy matters more here than in most refactors: this function implements invariant 7
(the run manifest must reconcile), and the failure mode that invariant exists to catch — a
confident table of four cars when forty matched — is exactly what a silently drifted second copy
would reintroduce.

**Every event's `text` is the exact string the CLI prints.** That is what makes the CLI a thin
pass-through and lets the refactor be verified by diffing stdout before and after, rather than by
reading the diff and hoping.

**Pipeline shape**, which follows directly from what the sites allow:

1. Search and filter — one page load per results page, no per-vehicle cost.
2. AutoCheck for **every** match — Carvana-hosted, no rate limit, gives every car a baseline.
3. Provisional rank on AutoCheck + price/mileage/KBB, then take the top N.
4. Carfax for **only** those N — DataDome allows ~6 per session, and one solved puzzle restored
   access for 7+ consecutive fetches, so a 12-car shortlist costs roughly one or two puzzles.
5. Merge pessimistically, score, report.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import browser, delivery, history, report as report_mod, scoring, search, vdp
from .cache import connect, stats as cache_stats
from .models import (
    STATUS_BLOCKED,
    STATUS_PARSED,
    HistoryReport,
    Listing,
    ScoredVehicle,
    SearchCriteria,
)

Emit = Callable[[dict], None]


class RunAborted(RuntimeError):
    """Raised internally when the caller's abort event is set, to unwind out of the stages.

    Never surfaces to a front end: `execute` catches it and returns a partial RunResult so
    whatever was already fetched still gets scored and reported.
    """


@dataclass
class RunOptions:
    """Everything a run needs, independent of argparse.

    Field names deliberately mirror the CLI's argparse destinations so `from_namespace` is a
    straight copy and the two front ends cannot drift in what they mean by a flag.
    """

    make: str | None = None
    model: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    max_price: float | None = None
    max_miles: int | None = None
    # None resolves at run start to this machine's captured delivery location. See
    # `delivery.default_zip` — a hardcoded default would price other operators' searches against
    # the author's city.
    zip_code: str | None = None

    top_n: int = 12
    max_reports: int = 40
    limit: int | None = None
    max_pages: int = 8
    sort: str = "score"
    search_only: bool = False
    no_history: bool = False
    no_carfax: bool = False
    no_imperfections: bool = False
    unattended: bool = False
    assist_timeout: int = 240

    @classmethod
    def from_namespace(cls, args: Any) -> "RunOptions":
        """Build options from an argparse Namespace, ignoring flags the pipeline does not use."""
        return cls(**{
            name: getattr(args, name)
            for name in cls.__dataclass_fields__
            if hasattr(args, name)
        })

    def criteria(self) -> SearchCriteria:
        """The immutable search criteria this run scores against."""
        return SearchCriteria(
            make=self.make, model=self.model,
            year_min=self.year_min, year_max=self.year_max,
            max_price=self.max_price, max_miles=self.max_miles,
            zip_code=self.zip_code, top_n=self.top_n, max_reports=self.max_reports,
        )

    def has_criterion(self) -> bool:
        """Whether at least one real search criterion was supplied.

        Mirrors the CLI's validation exactly, including the deliberate omission of `zip_code`:
        it has a default, so counting it would make every empty run look valid.
        """
        return any([self.make, self.model, self.year_min, self.year_max,
                    self.max_price, self.max_miles])


@dataclass
class RunResult:
    """The full outcome of a run — everything the CLI used to throw away.

    `exit_code` follows the CLI's contract: 0 success, 1 nothing matched or the manifest failed
    to reconcile.
    """

    options: RunOptions
    criteria: SearchCriteria
    manifest: report_mod.RunManifest
    exit_code: int = 0
    listings: list[Listing] = field(default_factory=list)
    histories: dict[str, HistoryReport] = field(default_factory=dict)
    imperfection_counts: dict[str, int | None] = field(default_factory=dict)
    shortlist_vins: set[str] = field(default_factory=set)
    scored: list[ScoredVehicle] = field(default_factory=list)
    anchor_info: dict[str, Any] = field(default_factory=dict)
    search_stats: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    report_path: Path | None = None
    cache_stats: dict[str, int] = field(default_factory=dict)
    aborted: bool = False
    config: dict[str, Any] = field(default_factory=dict)


def _listing_lines(listings: list[Listing]) -> str:
    """Compact inventory dump for --search-only, as one printable block."""
    lines = [f"\n{len(listings)} matching listing(s):\n"]
    for index, listing in enumerate(listings, start=1):
        kbb = (f"  kbb ${listing.kbb_value:,.0f}"
               f" ({listing.price_vs_kbb:+,.0f})" if listing.kbb_value else "")
        lines.append(f"  {index:>3}. {listing.label[:42]:<42} ${listing.landed_price:>9,.0f}"
                     f"  {listing.mileage:>7,} mi{kbb}")
        lines.append(f"       {listing.vin}  {listing.listing_url}")
    return "\n".join(lines)


def provisional_rank(
    listings: list[Listing],
    histories: dict[str, HistoryReport],
    criteria: SearchCriteria,
    config: dict,
) -> list[Listing]:
    """Order matches by an AutoCheck-only score to choose who gets a Carfax fetch.

    Uses the same scoring machinery as the final ranking, so the shortlist is chosen on the same
    basis it will later be judged on — just without the Carfax-only signals.
    """
    pairs = [(listing, histories[listing.vin], None) for listing in listings
             if listing.vin in histories]
    scored, _ = scoring.score_all(pairs, criteria, config)

    def provisional_key(vehicle):
        # score is None for incomplete histories (always, at this stage), so rank on the
        # component average instead — it reflects the signals we do have.
        components = vehicle.components
        average = sum(components.values()) / len(components) if components else 0.0
        return (-average, vehicle.listing.landed_price)

    ordered = sorted(scored, key=provisional_key)
    return [vehicle.listing for vehicle in ordered]


def _check_abort(abort: threading.Event | None) -> None:
    """Raise RunAborted if the caller asked to stop.

    Called at loop tops only. The deliberate `human_pause` sleeps between vehicles are plain
    `time.sleep` and cannot be interrupted, so a cancel takes effect at the next vehicle boundary
    rather than immediately — front ends should say so instead of implying it is instant.
    """
    if abort is not None and abort.is_set():
        raise RunAborted()


def _stage_one_search(
    page,
    options: RunOptions,
    criteria: SearchCriteria,
    manifest: report_mod.RunManifest,
    emit: Emit,
) -> tuple[list[Listing], dict]:
    """Load and filter inventory, recording every count on the manifest."""
    emit({"kind": "stage", "n": 1, "of": 4, "name": "search",
          "text": f"\n[1/4] searching: {criteria.describe()}"})
    listings, search_stats = search.collect_listings(
        page, criteria, max_pages=options.max_pages, hard_limit=options.limit)

    manifest.pages_loaded = search_stats["pages_loaded"]
    manifest.raw_records = search_stats["raw_records"]
    manifest.parsed_listings = search_stats["parsed_listings"]
    manifest.filtered_out = search_stats["filtered_out"]
    manifest.matched = search_stats["matched"]
    manifest.matched_before_limit = search_stats.get("matched_before_limit", 0)
    manifest.dropped_by_limit = search_stats.get("dropped_by_limit", 0)

    if manifest.dropped_by_limit:
        _warn(manifest, emit,
              f"--limit {options.limit} dropped {manifest.dropped_by_limit} of "
              f"{manifest.matched_before_limit} matching vehicles before any history was read")
    if search_stats.get("pagination_effective") is False:
        _warn(manifest, emit,
              f"pagination stopped early: {search_stats.get('stopped_because')}")
    if (criteria.zip_code and search_stats.get("priced_zip")
            and search_stats["priced_zip"] != criteria.zip_code):
        # Name the remedy. The zip IS settable — it needs a complete location captured from
        # Carvana's own picker during login, because a partial one is discarded and the cookie is
        # session-scoped. Saying only "prices reflect X" left this looking unfixable for a while.
        saved = delivery.load()
        if saved:
            remedy = (f"a saved location exists ({delivery.describe(saved)}) but Carvana priced "
                      "against something else — re-run Chrome login to refresh it")
        else:
            remedy = ("no delivery location is saved — run Chrome login and set the ZIP in "
                      "Carvana's own location picker, and every later run will use it")
        _warn(manifest, emit,
              f"prices reflect zip {search_stats['priced_zip']}, not {criteria.zip_code}: "
              f"{remedy}")

    emit({"kind": "search_done", "matched": manifest.matched,
          "parsed": manifest.parsed_listings, "stats": dict(search_stats),
          "text": f"      {manifest.matched} matched of {manifest.parsed_listings} parsed"})
    return listings, search_stats


def _warn(manifest: report_mod.RunManifest, emit: Emit, message: str) -> None:
    """Record a manifest warning and surface it as an event.

    The manifest keeps the authoritative list (it is printed with the report); the event exists so
    a GUI can show the warning while the run is still going rather than only at the end.
    """
    manifest.warnings.append(message)
    emit({"kind": "warning", "message": message})


def _stage_two_autocheck(
    context,
    page,
    connection,
    options: RunOptions,
    listings: list[Listing],
    manifest: report_mod.RunManifest,
    histories: dict[str, HistoryReport],
    imperfection_counts: dict[str, int | None],
    emit: Emit,
    abort: threading.Event | None,
) -> None:
    """AutoCheck for every match — Carvana-hosted, so no rate limit to budget around."""
    if options.no_history:
        emit({"kind": "stage", "n": 2, "of": 4, "name": "autocheck", "skipped": True,
              "text": "\n[2/4] --no-history: skipping all report fetches"})
        return

    budget = min(len(listings), options.max_reports)
    emit({"kind": "stage", "n": 2, "of": 4, "name": "autocheck", "total": budget,
          "text": f"\n[2/4] AutoCheck for {budget} vehicle(s) "
                  f"(Carvana-hosted, no rate limit observed)"})

    # A vehicle with no history never reaches `scored`, so it lands in NO bucket — not ranked, not
    # held out, not disqualified. Left unwarned, a run that matched 65 cars presents a table of 40
    # and reports that its counts reconcile. That is precisely the silent narrowing the manifest
    # exists to expose, and it is worse than the --limit case because --max-reports has a default:
    # the operator need never have chosen it.
    manifest.dropped_by_max_reports = len(listings) - budget
    if manifest.dropped_by_max_reports:
        _warn(manifest, emit,
              f"--max-reports {options.max_reports} left {manifest.dropped_by_max_reports} of "
              f"{len(listings)} matching vehicles with no history at all, so they appear in NO "
              f"section below — raise --max-reports to include them")

    for index, listing in enumerate(listings[:budget], start=1):
        _check_abort(abort)
        emit({"kind": "vehicle", "stage": "autocheck", "i": index, "of": budget,
              "vin": listing.vin, "label": listing.label,
              "text": f"  [{index}/{budget}] {listing.label} {listing.vin}"})

        report, from_cache = history.get_or_fetch(
            context, page, listing, connection, want_carfax=False,
            allow_manual_assist=False, verbose=True)
        histories[listing.vin] = report
        if from_cache:
            manifest.from_cache += 1
        if getattr(report, "status", None) == STATUS_PARSED:
            manifest.autocheck_parsed += 1
        elif getattr(report, "status", None) == STATUS_BLOCKED:
            # Counted against AutoCheck, not Carfax. This stage never touches Carfax, so the old
            # carfax_blocked increment here misattributed the failure and inflated a counter that
            # feeds reconciliation_problems() and the exit code.
            manifest.autocheck_blocked += 1
        else:
            manifest.history_unavailable += 1

        if not options.no_imperfections:
            imperfections = vdp.fetch_imperfections(page, listing.stock_number)
            imperfection_counts[listing.vin] = (
                len(imperfections) if imperfections is not None else None)
            if imperfections:
                emit({"kind": "imperfections", "vin": listing.vin,
                      "count": len(imperfections),
                      "text": f"    [vdp] imperfections: "
                              f"{vdp.summarize_imperfections(imperfections)}"})
        browser.human_pause(2.0, 5.0)


def _stage_three_carfax(
    context,
    page,
    connection,
    options: RunOptions,
    criteria: SearchCriteria,
    config: dict,
    listings: list[Listing],
    manifest: report_mod.RunManifest,
    histories: dict[str, HistoryReport],
    emit: Emit,
    abort: threading.Event | None,
) -> set[str]:
    """Carfax for the provisional top N only — DataDome allows roughly six per session."""
    if options.no_history or not histories:
        return set()
    if options.no_carfax:
        emit({"kind": "stage", "n": 3, "of": 4, "name": "carfax", "skipped": True,
              "text": "\n[3/4] --no-carfax: skipping Carfax; "
                      "nothing will enter the main ranking"})
        return set()

    shortlist = provisional_rank(listings, histories, criteria, config)[:options.top_n]
    shortlist_vins = {listing.vin for listing in shortlist}
    manifest.shortlisted = len(shortlist)

    mode = "unattended — will defer blocks" if options.unattended else "will pause for puzzles"
    emit({"kind": "stage", "n": 3, "of": 4, "name": "carfax", "total": len(shortlist),
          "unattended": options.unattended,
          "text": f"\n[3/4] Carfax for the top {len(shortlist)} "
                  f"(DataDome allows ~6/session; {mode})"})

    def on_challenge(label: str, timeout_s: int) -> None:
        """Surface a DataDome puzzle the moment it appears, not after it resolves."""
        emit({"kind": "challenge", "label": label, "timeout_s": timeout_s})

    attempted: list[str] = []
    for index, listing in enumerate(shortlist, start=1):
        _check_abort(abort)
        emit({"kind": "vehicle", "stage": "carfax", "i": index, "of": len(shortlist),
              "vin": listing.vin, "label": listing.label,
              "carfax_url": listing.carfax_url,
              "text": f"  [{index}/{len(shortlist)}] {listing.label} {listing.vin}"})
        manifest.carfax_attempted += 1
        attempted.append(listing.vin)

        # `page` is required even here: on a cache miss get_or_fetch still fetches AutoCheck
        # first, and that call needs a live page.
        report, _from_cache = history.get_or_fetch(
            context, page, listing, connection, want_carfax=True,
            allow_manual_assist=not options.unattended,
            assist_timeout_s=options.assist_timeout, verbose=True,
            on_challenge=on_challenge, abort=abort)
        histories[listing.vin] = report

        if not history.has_carfax(report):
            emit({"kind": "blocked", "vin": listing.vin, "label": listing.label,
                  "carfax_url": listing.carfax_url})
        for conflict in getattr(report, "conflicts", []) or []:
            emit({"kind": "conflict", "vin": listing.vin, "message": conflict})
        browser.human_pause()

    # Derived, not incremented. A blocked Carfax fetch does NOT yield a blocked report: on a cache
    # miss, merge_reports finds AutoCheck to be the only parsed vendor and returns it unmodified,
    # so the merged report reads vendor="autocheck"/status="parsed". The old per-vehicle branch
    # therefore matched neither case and carfax_blocked silently stayed at zero while cars were
    # being blocked. Deriving from has_carfax cannot drift, and stays correct after a paste.
    manifest.carfax_parsed = sum(1 for vin in attempted if history.has_carfax(histories.get(vin)))
    manifest.carfax_blocked = len(attempted) - manifest.carfax_parsed

    return shortlist_vins


def execute(
    options: RunOptions,
    emit: Emit = lambda event: None,
    abort: threading.Event | None = None,
) -> RunResult:
    """Run the full four-stage pipeline.

    Args:
        options: The run's criteria and control knobs.
        emit: Called with each progress event. Events carrying a `text` key hold the exact string
            the CLI prints for that step.
        abort: Set to request an early stop. Checked at each vehicle boundary and passed into the
            manual-assist wait; whatever was already fetched is still scored and reported.

    Returns:
        A RunResult carrying the scored vehicles, the manifest, the rendered report and the
        process exit code.

    Raises:
        browser.ProfileLockedError: Another Chrome is using the dedicated profile.
        rsc.PayloadShapeError: The first results page yielded no extractable records.
        FileNotFoundError: config/scoring.json is missing.
    """
    # Resolved here rather than in each front end so the CLI, the app and the worker cannot drift.
    # Mutating `options` is intended: every caller stores it as the record of what the run used, so
    # it must show the zip that was actually requested.
    if options.zip_code is None:
        options.zip_code = delivery.default_zip()

    criteria = options.criteria()
    config = scoring.load_config()
    manifest = report_mod.RunManifest(criteria=criteria.describe())
    result = RunResult(options=options, criteria=criteria, manifest=manifest, config=config)

    histories: dict[str, HistoryReport] = {}
    imperfection_counts: dict[str, int | None] = {}
    result.histories = histories
    result.imperfection_counts = imperfection_counts

    # Opened on the thread that runs the pipeline. sqlite3 connections are thread-bound
    # (cache.connect does not pass check_same_thread=False) and the app runs this on a worker
    # thread while the HTTP thread reads state, so this connection must never leave this call.
    connection = connect()
    try:
        try:
            with browser.session() as context:
                page = context.pages[0] if context.pages else context.new_page()

                listings, search_stats = _stage_one_search(
                    page, options, criteria, manifest, emit)
                result.listings = listings
                result.search_stats = search_stats

                if options.search_only:
                    emit({"kind": "listings", "count": len(listings),
                          "text": _listing_lines(listings)})
                    result.exit_code = 0
                    return result
                if not listings:
                    emit({"kind": "empty", "text": "\nNo vehicles matched. Nothing to rank."})
                    emit({"kind": "manifest", "text": "\n".join(manifest.lines())})
                    result.exit_code = 1
                    return result

                _stage_two_autocheck(context, page, connection, options, listings, manifest,
                                     histories, imperfection_counts, emit, abort)
                result.shortlist_vins = _stage_three_carfax(
                    context, page, connection, options, criteria, config, listings, manifest,
                    histories, emit, abort)
        except RunAborted:
            # Deliberately not re-raised: whatever was already fetched still gets scored, and the
            # manifest makes the shortfall explicit rather than presenting a partial set as whole.
            result.aborted = True
            emit({"kind": "aborted", "text": "\nCancelled. Progress is cached; "
                                             "re-run to continue where this left off."})

        _score_and_report(result, connection, emit)
        return result
    finally:
        connection.close()


def _score_and_report(result: RunResult, connection, emit: Emit) -> None:
    """Stage 4 — score whatever was gathered, render the report, set the exit code.

    Runs after the browser has closed, and runs even for an aborted run: a partial ranking that
    says so is more useful than nothing, and the manifest makes the shortfall explicit.
    """
    emit({"kind": "stage", "n": 4, "of": 4, "name": "scoring", "text": "\n[4/4] scoring"})

    pairs = [(listing, result.histories[listing.vin],
              result.imperfection_counts.get(listing.vin))
             for listing in result.listings if listing.vin in result.histories]
    scored, anchor_info = scoring.score_all(pairs, result.criteria, result.config)
    for vehicle in scored:
        vehicle.carfax_attempted = vehicle.listing.vin in result.shortlist_vins

    result.scored = scored
    result.anchor_info = anchor_info
    result.manifest.anchor_note = anchor_info["note"]
    if not anchor_info["anchored"]:
        _warn(result.manifest, emit, anchor_info["note"])

    # render() mutates the manifest (ranked / needs_carfax / disqualified / conflicts), so it must
    # run before anything reads those counters or computes the exit code.
    body = report_mod.render(scored, result.criteria, result.manifest,
                             sort_by=result.options.sort)
    result.body = body
    emit({"kind": "report", "text": "\n" + body})

    path = report_mod.write_markdown(body)
    result.report_path = path
    emit({"kind": "written", "path": str(path), "text": f"report written: {path}"})

    result.cache_stats = cache_stats(connection)
    emit({"kind": "cache", "stats": dict(result.cache_stats),
          "text": f"history cache: {result.cache_stats}"})

    result.exit_code = report_mod.manifest_exit_code(result.manifest)
