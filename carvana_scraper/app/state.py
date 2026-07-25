"""Shared state between the pipeline worker thread and the HTTP server threads.

One lock guards everything. The worker thread writes; server threads only ever read a snapshot
built inside the lock, so a poll can never observe a half-updated run.

Why a worker thread at all: `browser.session()` uses Playwright's **sync** API, whose objects are
bound to the thread that created them, and `sqlite3` connections are likewise thread-bound
(`cache.connect` does not pass `check_same_thread=False`). So the whole pipeline runs on one thread
that owns both, and this object is the only thing the HTTP side touches.
"""

from __future__ import annotations

import threading
from typing import Any

from .. import report as report_mod
from ..models import HistoryReport, ScoredVehicle
from ..pipeline import RunOptions, RunResult
from . import serialize

# Events are for a live progress feed, not an audit log — the report and manifest are the record.
# Bounded so a long run cannot grow the response body without limit.
MAX_EVENTS = 400

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_LOGIN = "login"
STATUS_DONE = "done"
STATUS_ERROR = "error"


class AppState:
    """The app's single mutable state object."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.status: str = STATUS_IDLE
        self.options: RunOptions | None = None
        self.result: RunResult | None = None
        self.abort: threading.Event | None = None

        self.stage: dict[str, Any] = {}
        self.progress: dict[str, Any] = {}
        self.events: list[dict] = []
        self.warnings: list[str] = []
        self.challenge: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.review: dict[str, Any] | None = None
        self.review_running: bool = False
        self.ingested: list[dict[str, Any]] = []
        # A set, not a counter: pasting the same report twice must not inflate the manifest.
        self.pasted_vins: set[str] = set()
        self._sequence = 0

    # ---- run lifecycle -------------------------------------------------------------------

    def is_busy(self) -> bool:
        """Whether a run or login is in flight.

        Only one at a time is possible: the dedicated Chrome profile is single-instance
        (.browser-profile/SingletonLock), so a second concurrent session would fail anyway.
        """
        with self._lock:
            return self.status in (STATUS_RUNNING, STATUS_LOGIN)

    def begin_run(self, options: RunOptions) -> threading.Event:
        """Reset for a new run and return its abort event."""
        with self._lock:
            self.status = STATUS_RUNNING
            self.options = options
            self.result = None
            self.abort = threading.Event()
            self.stage = {}
            self.progress = {}
            self.events = []
            self.warnings = []
            self.challenge = None
            self.error = None
            self.review = None
            self.review_running = False
            self.ingested = []
            self.pasted_vins = set()
            return self.abort

    def begin_login(self) -> None:
        with self._lock:
            self.status = STATUS_LOGIN
            self.error = None
            self.events = []

    def finish_login(self) -> None:
        with self._lock:
            if self.status == STATUS_LOGIN:
                self.status = STATUS_IDLE

    def finish_run(self, result: RunResult) -> None:
        with self._lock:
            self.result = result
            self.status = STATUS_DONE
            self.challenge = None
            self.warnings = list(result.manifest.warnings)

    def fail_run(self, kind: str, message: str, hint: str = "") -> None:
        """Record a failure as a first-class UI state rather than letting it surface as a stack."""
        with self._lock:
            self.status = STATUS_ERROR
            self.challenge = None
            self.error = {"kind": kind, "message": message, "hint": hint}

    def request_abort(self) -> bool:
        with self._lock:
            if self.abort is None or self.status != STATUS_RUNNING:
                return False
            self.abort.set()
            return True

    # ---- event ingestion ----------------------------------------------------------------

    def record_event(self, event: dict) -> None:
        """Absorb one pipeline event. Called from the worker thread only."""
        with self._lock:
            kind = event.get("kind")

            if kind == "stage":
                self.stage = {"n": event.get("n"), "of": event.get("of"),
                              "name": event.get("name"), "total": event.get("total"),
                              "skipped": bool(event.get("skipped"))}
                self.progress = {}
                self.challenge = None
            elif kind == "vehicle":
                self.progress = {"i": event.get("i"), "of": event.get("of"),
                                 "vin": event.get("vin"), "label": event.get("label")}
                self.challenge = None
            elif kind == "challenge":
                self.challenge = {"label": event.get("label"),
                                  "timeout_s": event.get("timeout_s")}
            elif kind == "warning":
                message = event.get("message", "")
                if message and message not in self.warnings:
                    self.warnings.append(message)

            self._append_event(event)

    def _append_event(self, event: dict) -> None:
        """Store a display-friendly copy of the event. Assumes the lock is held."""
        self._sequence += 1
        entry = {"seq": self._sequence, "kind": event.get("kind")}
        for key in ("text", "message", "vin", "label", "i", "of", "n", "name", "timeout_s"):
            if key in event:
                entry[key] = event[key]
        self.events.append(entry)
        if len(self.events) > MAX_EVENTS:
            del self.events[:len(self.events) - MAX_EVENTS]

    def note(self, kind: str, text: str) -> None:
        """Append an app-generated event (not from the pipeline)."""
        with self._lock:
            self._append_event({"kind": kind, "text": text})

    # ---- review + ingest ----------------------------------------------------------------

    def set_review_running(self, running: bool) -> None:
        with self._lock:
            self.review_running = running

    def set_review(self, review: dict[str, Any]) -> None:
        with self._lock:
            self.review = review
            self.review_running = False

    def record_ingest(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.ingested.append(entry)
            self.pasted_vins.add(entry["vin"])

    def replace_scored(self, scored: list[ScoredVehicle], body: str,
                       manifest: report_mod.RunManifest) -> None:
        """Swap in a rescored result after a report was pasted in.

        The review is cleared deliberately: it was written against the previous evidence, and a
        stale "best pick" shown beside changed history would be worse than none.
        """
        with self._lock:
            if self.result is None:
                return
            self.result.scored = scored
            self.result.body = body
            self.result.manifest = manifest
            self.warnings = list(manifest.warnings)
            self.review = None

    def history_for(self, vin: str) -> HistoryReport | None:
        with self._lock:
            if self.result is None:
                return None
            return self.result.histories.get(vin.upper())

    def listing_for(self, vin: str):
        with self._lock:
            if self.result is None:
                return None
            for listing in self.result.listings:
                if listing.vin == vin.upper():
                    return listing
            return None

    # ---- snapshot -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe view of everything the browser needs, built inside the lock."""
        with self._lock:
            payload: dict[str, Any] = {
                "status": self.status,
                "stage": dict(self.stage),
                "progress": dict(self.progress),
                "events": [dict(event) for event in self.events],
                "warnings": list(self.warnings),
                "challenge": dict(self.challenge) if self.challenge else None,
                "error": dict(self.error) if self.error else None,
                "review": self.review,
                "review_running": self.review_running,
                "ingested": [dict(entry) for entry in self.ingested],
                "criteria": None,
                "sort": self.options.sort if self.options else "score",
                "anchored": None,
                "report_path": None,
                "exit_code": None,
                "aborted": False,
                "manifest": None,
                "ranked": [],
                "needs_carfax": [],
                "disqualified": [],
                "cache_stats": {},
            }

            if self.options is not None:
                payload["criteria"] = self.options.criteria().describe()

            result = self.result
            if result is None:
                return payload

            # `_sort_key` is private to report.py but this is the same package, and reusing it is
            # what guarantees the table order matches the rendered markdown exactly.
            sort_key = report_mod._sort_key(result.options.sort)
            # A run that deliberately skipped the history stages cannot be judged by the
            # reconciliation checks, which assume the full pipeline ran.
            full_run = not (result.options.search_only or result.options.no_history)
            payload.update({
                "criteria": result.criteria.describe(),
                "sort": result.options.sort,
                "search_only": result.options.search_only,
                "anchored": result.anchor_info.get("anchored"),
                "anchor_note": result.anchor_info.get("note", ""),
                "report_path": str(result.report_path) if result.report_path else None,
                "exit_code": result.exit_code,
                "aborted": result.aborted,
                "manifest": serialize.manifest_to_dict(result.manifest, reconcile=full_run),
                "cache_stats": dict(result.cache_stats),
                "report_body": result.body,
            })
            payload.update(serialize.buckets(
                result.scored, sort_key=sort_key,
                carfax_skipped=result.options.no_carfax or result.options.no_history))
            return payload
