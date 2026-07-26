"""Claim ranking jobs from the shared web app and run them on this machine.

The scraping half of this tool cannot move to a serverless host: it needs headful real Chrome with
an aged persistent profile, a human at the keyboard to clear a DataDome puzzle, minute-long runs
with deliberate pacing, a single-instance profile lock, SQLite on a writable disk, and a local
`claude` CLI for the reviewer. So the web app holds the queue and the results, and *this* runs
wherever the operator actually is.

**One orchestration.** This is a third thin caller of `pipeline.execute`, alongside `cli.run` and
`app/runner.start_run` — never a second copy of the four stages. It reuses `AppState` verbatim:
`record_event` is already the emit callback shape, and `snapshot()` is already the exact payload
the browser renders, so there is no serialization code here and the web UI consumes the same
contract the local app does.

**Jobs belong to the queue, not to a machine.** Whichever worker is running takes the next one, so
a visitor to the site needs nothing installed — they submit, and the laptop actually running this
does the browser work. The worker token authenticates the machine; the site PIN authenticates the
person, and neither substitutes for the other.

Networking is `urllib.request`, so the "Playwright only" dependency rule still holds.

    export CARVANA_WEB_URL=https://…
    export VERCEL_AUTOMATION_BYPASS_SECRET=…      # else Deployment Protection blocks the API
    python3 -m carvana_scraper.worker
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import browser, pipeline, report as report_mod, rsc
from .app import review as review_mod
from .app.server import options_from_payload as _coerce_options
from .app.state import AppState
from .cache import DEFAULT_RAW_DIR
from .pipeline import RunOptions

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = PROJECT_ROOT / ".worker-token"

DEFAULT_POLL_INTERVAL_S = 5.0
# A run takes minutes, so a push every few seconds is a live feed without hammering the database.
PROGRESS_INTERVAL_S = 3.0
HTTP_TIMEOUT_S = 30
VENDORS = ("carfax", "autocheck")


class WorkerError(RuntimeError):
    """A request to the web app failed in a way the operator needs to see."""


# ---- token ---------------------------------------------------------------------------------


def load_or_create_token(path: Path | str | None = None) -> str:
    """This machine's worker token, generating one on first run.

    Written 0600 and gitignored. It is the machine's identity, so a leaked token lets someone else
    claim this operator's jobs and post results as them.

    `path` defaults to the module's TOKEN_PATH read *at call time*, not bound as an argument
    default: a default is evaluated once at import, so tests patching `worker.TOKEN_PATH` would
    silently write a real token into the repo instead.
    """
    target = Path(path if path is not None else TOKEN_PATH)
    if target.is_file():
        token = target.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(token + "\n", encoding="utf-8")
    target.chmod(0o600)
    return token


# ---- transport -----------------------------------------------------------------------------


class Client:
    """Thin JSON client for the web app's worker routes."""

    def __init__(self, base_url: str, token: str, bypass: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.bypass = bypass

    def post(self, path: str, payload: dict) -> dict:
        """POST JSON and return the decoded response.

        Raises:
            WorkerError: On any transport or non-2xx response.
        """
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise WorkerError(f"{path} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WorkerError(f"{path} unreachable: {exc.reason}") from exc
        return json.loads(body) if body.strip() else {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        if self.bypass:
            # Vercel's Deployment Protection gates the API routes too, not just pages. Without
            # this header the worker is served the password interstitial and every call fails as
            # "expected JSON, got HTML" — which reads like a bug in the app rather than a missing
            # secret. See Protection Bypass for Automation in project settings.
            headers["x-vercel-protection-bypass"] = self.bypass
        return headers

    # -- routes --

    def register(self, label: str) -> dict:
        return self.post("/api/worker/register", {"label": label})

    def claim(self) -> dict | None:
        """The next job for this worker, or None when the queue is empty."""
        return self.post("/api/worker/claim", {}).get("run")

    def progress(self, run_id: str, snapshot: dict) -> None:
        self.post("/api/worker/progress", {"run_id": run_id, "progress": snapshot})

    def complete(self, run_id: str, snapshot: dict, reports: list[dict]) -> None:
        self.post("/api/worker/complete",
                  {"run_id": run_id, "result": snapshot, "reports": reports})

    def fail(self, run_id: str, kind: str, message: str, hint: str = "") -> None:
        self.post("/api/worker/complete",
                  {"run_id": run_id,
                   "error": {"kind": kind, "message": message, "hint": hint}})


# ---- options -------------------------------------------------------------------------------


def options_from_payload(payload: dict) -> RunOptions:
    """Build RunOptions from a claimed job, refusing anything unrecognized.

    Unknown keys are an error rather than ignored. The web form's schema and this dataclass are
    written in different languages and can drift; silently dropping a field would run *different
    criteria* than the person asked for and still report success — the same silent-narrowing
    failure the run manifest exists to catch.

    Coercion and the "at least one criterion" check are delegated to the local app's parser so the
    two front ends cannot disagree about what a payload means.

    Raises:
        ValueError: On an unknown key, an unconvertible value, or no real criterion.
    """
    unknown = sorted(set(payload) - set(RunOptions.__dataclass_fields__))
    if unknown:
        raise ValueError(
            f"job carries field(s) this worker does not understand: {', '.join(unknown)}. "
            "Update the worker, or the web app is sending criteria that would be ignored.")
    return _coerce_options(payload)


# ---- publishing ----------------------------------------------------------------------------


class ProgressPusher:
    """Emit callback that records into AppState and pushes a throttled snapshot upstream."""

    def __init__(self, client: Client, run_id: str, state: AppState,
                 interval: float = PROGRESS_INTERVAL_S) -> None:
        self.client = client
        self.run_id = run_id
        self.state = state
        self.interval = interval
        self._last = 0.0

    def __call__(self, event: dict) -> None:
        self.state.record_event(event)
        # A challenge goes up immediately: the whole point is telling the operator to go look at
        # the Chrome window, and waiting out the throttle wastes the assist timeout they have.
        if event.get("kind") == "challenge" or self._due():
            self.push()

    def _due(self) -> bool:
        return (time.monotonic() - self._last) >= self.interval

    def push(self) -> None:
        """Send the current snapshot, swallowing transport failures.

        A dropped progress update must never abort a run that is otherwise fine — the operator
        would lose a real browser session and any un-cached Carfax fetches with it.
        """
        self._last = time.monotonic()
        try:
            self.client.progress(self.run_id, self.state.snapshot())
        except WorkerError as exc:
            print(f"  [worker] progress update failed (run continues): {exc}", file=sys.stderr)


def collect_reports(result, raw_dir: Path | str | None = None) -> list[dict]:
    """Every archived report this run touched, as rows for the web app.

    Reuses the reviewer's reader rather than globbing: it already tolerates both naming schemes in
    `cache/raw/`, so a pasted report and a scraped one look identical here.

    `raw_dir` resolves at call time for the same reason `load_or_create_token`'s path does — an
    argument default is bound once at import, which makes the directory unpatchable from a test.
    """
    raw_dir = Path(raw_dir if raw_dir is not None else DEFAULT_RAW_DIR)
    rows = []
    for vin in sorted(result.histories):
        for vendor in VENDORS:
            body = review_mod._read_report(vin, vendor, raw_dir)
            if body:
                rows.append({"vin": vin, "vendor": vendor, "body": body})
    return rows


def maybe_review(result, state: AppState) -> None:
    """Run the local-Claude reviewer if this machine has one. Never fatal.

    Most machines running a worker will not have the `claude` CLI, and that must degrade to a note
    rather than a failed run — the deterministic ranking is the product and stands on its own.
    """
    if shutil.which("claude") is None:
        state.note("review", "Report review skipped: the `claude` CLI is not installed here.")
        return
    try:
        vehicles = review_mod.select_vehicles(
            result.scored, report_mod._sort_key(result.options.sort),
            review_mod.DEFAULT_REVIEW_COUNT)
        if not vehicles:
            state.note("review", "Report review skipped: no vehicle has both reports yet.")
            return
        state.set_review(review_mod.run_review(vehicles))
        state.note("review", f"Review complete: {len(state.review['findings'])} finding(s).")
    except Exception as exc:  # noqa: BLE001 - a review failure must not fail the run
        state.set_review({"error": f"{type(exc).__name__}: {exc}"})
        state.note("review", f"Review unavailable: {exc}")


# ---- the job -------------------------------------------------------------------------------

# Mirrors app/runner._run_worker so the two front ends give the operator the same remediation for
# the same failure. The messages are the ones that were written against real failures.
_FAILURES: tuple[tuple[type[Exception], str, str], ...] = (
    (browser.ProfileLockedError, "profile_locked",
     "Close the other Chrome window using .browser-profile, or run the rm -f command above, "
     "then try again."),
    (rsc.PayloadShapeError, "payload_shape",
     "Carvana's page structure changed. Re-run the discovery in docs/RECON.md before trusting "
     "any result."),
    (FileNotFoundError, "missing_config",
     "config/scoring.json is required — it holds the weights and disqualifiers."),
)


def run_one(client: Client, job: dict) -> None:
    """Execute one claimed job and publish its outcome. Never raises."""
    run_id = job["id"]
    state = AppState()

    try:
        options = options_from_payload(job.get("options") or {})
    except ValueError as exc:
        print(f"  [worker] refusing job {run_id}: {exc}", file=sys.stderr)
        client.fail(run_id, "invalid_options", str(exc),
                    "The web app queued criteria this worker cannot run.")
        return

    print(f"\n[worker] running job {run_id}: {options.criteria().describe()}")
    abort = state.begin_run(options)
    emit = ProgressPusher(client, run_id, state)

    try:
        result = pipeline.execute(options, emit=emit, abort=abort)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a published state
        kind, hint = _classify(exc)
        state.fail_run(kind, str(exc), hint)
        _safe_publish(client, run_id, lambda: client.fail(run_id, kind, str(exc), hint))
        print(f"  [worker] job {run_id} failed: {kind}: {exc}", file=sys.stderr)
        return

    state.finish_run(result)
    emit.push()
    maybe_review(result, state)
    _safe_publish(client, run_id,
                  lambda: client.complete(run_id, state.snapshot(), collect_reports(result)))
    print(f"  [worker] job {run_id} done (exit code {result.exit_code})")


def _classify(exc: Exception) -> tuple[str, str]:
    """Map an exception to the (kind, hint) the operator should see."""
    for exc_type, kind, hint in _FAILURES:
        if isinstance(exc, exc_type):
            return kind, hint
    return type(exc).__name__, traceback.format_exc(limit=6)


def _safe_publish(client: Client, run_id: str, publish) -> None:
    """Publish an outcome, reporting rather than raising if the site is unreachable.

    The run already happened and its reports are cached locally, so losing the upload is
    recoverable — losing the worker loop to an exception is not.
    """
    try:
        publish()
    except WorkerError as exc:
        print(f"  [worker] could not publish job {run_id}: {exc}", file=sys.stderr)


# ---- entry point ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m carvana_scraper.worker",
        description="Run ranking jobs queued from the shared web app on this machine.")
    parser.add_argument("--once", action="store_true",
                        help="run a single job (or exit if the queue is empty) instead of looping")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S,
                        help=f"seconds between queue checks (default {DEFAULT_POLL_INTERVAL_S:g})")
    return parser


def _announce(info: dict, base_url: str) -> None:
    """Confirm this machine is serving, and offer to make it *this* browser's machine."""
    queued = info.get("queued") or 0
    print("\n" + "=" * 72)
    print(f"  Running as {info.get('label')!r}.")
    print(f"  {base_url} can now run searches.")
    print(f"  {queued} search(es) waiting." if queued else "  Queue is empty. Waiting…")

    link_path = info.get("link_path")
    if link_path:
        # A link rather than a code: nothing to read off one screen and type into another.
        print("")
        print("  To make YOUR searches run on THIS machine, open once in your browser:")
        print(f"    {base_url}{link_path}")
        print(f"  (expires in {info.get('link_ttl_minutes', 30)} minutes; restart for a new one)")
    elif info.get("claimed"):
        print("  This machine is claimed — its owner's searches run here.")

    print("")
    print("  Leave this window open. Ctrl-C to stop.")
    print("=" * 72 + "\n")


def main(argv: list[str] | None = None) -> int:
    """Poll for jobs until interrupted. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    base_url = os.environ.get("CARVANA_WEB_URL", "").strip()
    if not base_url:
        print("CARVANA_WEB_URL is not set — point it at the deployed app.", file=sys.stderr)
        return 2

    client = Client(base_url, load_or_create_token(),
                    os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or None)
    try:
        _announce(client.register(socket.gethostname()), client.base_url)
    except WorkerError as exc:
        print(f"Could not reach {base_url}: {exc}", file=sys.stderr)
        return 2

    try:
        while True:
            try:
                job = client.claim()
            except WorkerError as exc:
                # Transient by assumption: a laptop sleeps, wifi drops, a deploy restarts. Keep
                # polling rather than making the operator notice and relaunch.
                print(f"  [worker] queue check failed, retrying: {exc}", file=sys.stderr)
                job = None
            if job is not None:
                run_one(client, job)
                if args.once:
                    return 0
            elif args.once:
                print("[worker] nothing queued.")
                return 0
            else:
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("\n[worker] stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
