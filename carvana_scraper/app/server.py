"""Local HTTP server for the app. stdlib only, bound to loopback.

Deliberately minimal: static files, a JSON state endpoint the page polls, and a handful of POSTs.
No framework, no templating, no websockets — a poll every second is plenty for a run whose stages
take minutes, and it keeps the dependency list at "Playwright only".

Security posture for a local single-user tool: bound to 127.0.0.1 so nothing off-machine can reach
it, and cross-origin requests are refused so a page you happen to have open in another tab cannot
drive your browser profile or start scrapes.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import report as report_mod
from ..pipeline import RunOptions
from . import ingest as ingest_mod, review as review_mod, runner
from .state import AppState

STATIC_DIR = Path(__file__).resolve().parent / "static"
TAXONOMY_PATH = runner.TAXONOMY_PATH

HOST = "127.0.0.1"
MAX_BODY_BYTES = 2_000_000  # a pasted report is ~15 KB; this is generous and bounded

_CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8"}

# Flags the UI posts as booleans.
_BOOL_FIELDS = ("search_only", "no_history", "no_carfax", "no_imperfections", "unattended")
# Flags the UI posts as numbers. None means "not set", which is meaningful for limit.
_INT_FIELDS = ("year_min", "year_max", "max_miles", "top_n", "max_reports", "limit",
               "max_pages", "assist_timeout")


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def options_from_payload(payload: dict) -> RunOptions:
    """Build RunOptions from the browser form.

    `build_parser()` is deliberately not reused: `parser.error()` calls `sys.exit(2)`, which would
    terminate the server process instead of returning a 400.

    Raises:
        ValueError: If a value will not convert, or no real criterion was given.
    """
    fields: dict[str, Any] = {}
    for name in _INT_FIELDS:
        value = _as_int(payload.get(name))
        if value is not None:
            fields[name] = value
    fields["limit"] = _as_int(payload.get("limit"))

    max_price = _as_float(payload.get("max_price"))
    if max_price is not None:
        fields["max_price"] = max_price

    for name in ("make", "model"):
        value = (payload.get(name) or "").strip()
        if value:
            fields[name] = value

    zip_code = (payload.get("zip_code") or "").strip()
    if zip_code:
        fields["zip_code"] = zip_code

    sort = (payload.get("sort") or "score").strip()
    if sort not in report_mod.SORT_KEYS:
        raise ValueError(f"sort must be one of {list(report_mod.SORT_KEYS)}")
    fields["sort"] = sort

    for name in _BOOL_FIELDS:
        fields[name] = bool(payload.get(name))

    options = RunOptions(**fields)
    if not options.has_criterion():
        raise ValueError("Give at least one of make, model, year range, max price or max miles.")
    if (options.year_min is not None and options.year_max is not None
            and options.year_min > options.year_max):
        raise ValueError("Minimum year cannot be after maximum year.")
    return options


class Handler(BaseHTTPRequestHandler):
    """Request handler. One instance per request, on a pool thread."""

    server_version = "carvana-scraper-app"
    state: AppState
    login_done: threading.Event

    # ---- plumbing ------------------------------------------------------------------

    def log_message(self, format: str, *args) -> None:
        """Silence per-request logging; the pipeline's own output is what matters here."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Belt-and-braces against a stray page framing or sniffing this.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, hint: str = "") -> None:
        self._json({"error": message, "hint": hint}, status=status)

    def _origin_is_local(self) -> bool:
        """Refuse cross-origin requests.

        Without this, any page in any tab could POST to this server and start a scrape or drive the
        browser profile. Same-origin fetches from our own page send no Origin header (or send ours).
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in (f"http://{HOST}:{self.server.server_port}",
                          f"http://localhost:{self.server.server_port}")

    def _read_json(self) -> dict:
        """Read and parse a JSON request body.

        Raises:
            ValueError: On a missing, oversized or malformed body.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty request body")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body too large ({length} bytes)")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    # ---- routing -------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        route = self.path.split("?", 1)[0]
        if route == "/":
            return self._serve_static("index.html")
        if route in ("/app.css", "/app.js"):
            return self._serve_static(route.lstrip("/"))
        if route == "/api/state":
            return self._json(self.state.snapshot())
        if route == "/api/taxonomy":
            return self._serve_taxonomy()
        if route == "/api/config":
            return self._json({
                "sort_keys": list(report_mod.SORT_KEYS),
                "models": list(review_mod.ALLOWED_MODELS),
                "default_model": review_mod.DEFAULT_MODEL,
                "default_review_count": review_mod.DEFAULT_REVIEW_COUNT,
                "defaults": vars(RunOptions()),
                "min_report_chars": ingest_mod.MIN_REPORT_CHARS,
            })
        return self._error(404, f"no route {route}")

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_is_local():
            return self._error(403, "cross-origin requests are refused")

        route = self.path.split("?", 1)[0]
        handlers = {
            "/api/run": self._post_run,
            "/api/cancel": self._post_cancel,
            "/api/login": self._post_login,
            "/api/login/done": self._post_login_done,
            "/api/taxonomy/refresh": self._post_taxonomy_refresh,
            "/api/ingest": self._post_ingest,
            "/api/review": self._post_review,
        }
        handler = handlers.get(route)
        if handler is None:
            return self._error(404, f"no route {route}")
        try:
            handler()
        except ValueError as exc:
            self._error(400, str(exc))

    # ---- static --------------------------------------------------------------------

    def _serve_static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        # Defence in depth: `name` is only ever from the fixed route table above, but resolving and
        # re-checking the parent means a future route cannot turn into a path traversal.
        if not path.is_file() or path.parent != STATIC_DIR.resolve():
            return self._error(404, f"no asset {name}")
        self._send(200, path.read_bytes(),
                   _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def _serve_taxonomy(self) -> None:
        if not TAXONOMY_PATH.is_file():
            return self._error(
                503, "config/carvana-taxonomy.json is missing.",
                "Run `python3 tools/extract_taxonomy.py`, or use Refresh taxonomy.")
        self._send(200, TAXONOMY_PATH.read_bytes(), "application/json; charset=utf-8")

    # ---- actions -------------------------------------------------------------------

    def _post_run(self) -> None:
        if self.state.is_busy():
            return self._error(409, "a run is already in progress",
                               "Only one browser session can use the profile at a time.")
        options = options_from_payload(self._read_json())
        runner.start_run(self.state, options)
        self._json({"started": True, "criteria": options.criteria().describe()})

    def _post_cancel(self) -> None:
        if self.state.request_abort():
            # Said plainly because it is true: human_pause() uses time.sleep, which cannot be
            # interrupted, so the run stops at the next vehicle rather than instantly.
            return self._json({"cancelling": True,
                               "note": "Stopping after the current vehicle finishes."})
        self._json({"cancelling": False, "note": "Nothing is running."})

    def _post_login(self) -> None:
        if self.state.is_busy():
            return self._error(409, "a run or login is already in progress")
        self.login_done.clear()
        runner.start_login(self.state, self.login_done)
        self._json({"started": True})

    def _post_login_done(self) -> None:
        self.login_done.set()
        self._json({"done": True})

    def _post_taxonomy_refresh(self) -> None:
        if self.state.is_busy():
            return self._error(409, "a run or login is already in progress")
        runner.start_taxonomy_refresh(self.state)
        self._json({"started": True})

    def _post_ingest(self) -> None:
        payload = self._read_json()
        vin = (payload.get("vin") or "").strip()
        text = payload.get("text") or ""
        vendor = (payload.get("vendor") or "").strip() or None
        try:
            summary = runner.apply_paste(self.state, vin, text, vendor)
        except ingest_mod.IngestError as exc:
            return self._error(422, str(exc), getattr(exc, "hint", ""))
        self._json({"ok": True, "summary": summary})

    def _post_review(self) -> None:
        payload = self._read_json()
        if self.state.review_running:
            return self._error(409, "a review is already running")
        if self.state.result is None:
            return self._error(409, "no completed run to review")
        backend = (payload.get("backend") or "claude").strip()
        model = (payload.get("model") or review_mod.DEFAULT_MODEL).strip()
        count = _as_int(payload.get("count")) or review_mod.DEFAULT_REVIEW_COUNT
        runner.start_review(self.state, backend, model, count)
        self._json({"started": True})


def build_server(port: int = 0) -> ThreadingHTTPServer:
    """Create the server. Port 0 lets the OS pick a free one."""
    state = AppState()
    login_done = threading.Event()

    handler = type("BoundHandler", (Handler,), {"state": state, "login_done": login_done})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def serve(port: int = 0, open_browser: bool = True) -> None:
    """Run the app until interrupted."""
    server = build_server(port)
    url = f"http://{HOST}:{server.server_port}/"
    print(f"\nCarvana ranker UI: {url}")
    print("A separate Chrome window opens during a run — that is the scraper's own profile,")
    print("and you need to see it to clear any DataDome puzzle.")
    print("Ctrl-C to stop.\n")

    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.shutdown()
        server.server_close()
