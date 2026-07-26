"""Worker tests. Fully offline: no network, no browser, no `claude` CLI.

The transport is stubbed at `Client.post`, which is the single choke point every route goes
through, so these exercise the real request bodies the web app will receive.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from carvana_scraper import browser, pipeline, report as report_mod, rsc, worker
from carvana_scraper.pipeline import RunOptions, RunResult


def make_result(histories: dict | None = None, **over) -> RunResult:
    """A real RunResult, so publishing exercises the actual AppState.snapshot() path.

    A hand-rolled fake would drift from what the web app is actually sent — the payload shape is
    the contract between the worker and the UI, so it is worth building for real here.
    """
    options = RunOptions(make="Toyota", **over)
    criteria = options.criteria()
    return RunResult(options=options, criteria=criteria,
                     manifest=report_mod.RunManifest(criteria=criteria.describe()),
                     histories=histories or {})


class RecordingClient(worker.Client):
    """A Client that records calls instead of making them."""

    def __init__(self, claims=None, fail_on: set[str] | None = None) -> None:
        super().__init__("https://example.test", "tok", bypass="bypass-secret",
                         enroll="enroll-key")
        self.calls: list[tuple[str, dict]] = []
        self.extra_headers: list[tuple[str, dict[str, str]]] = []
        self._claims = list(claims or [])
        self._fail_on = fail_on or set()

    def post(self, path: str, payload: dict,
             extra_headers: dict[str, str] | None = None) -> dict:
        self.calls.append((path, payload))
        self.extra_headers.append((path, extra_headers or {}))
        if path in self._fail_on:
            raise worker.WorkerError(f"stubbed failure on {path}")
        if path == "/api/worker/claim":
            return {"run": self._claims.pop(0) if self._claims else None}
        if path == "/api/worker/register":
            return {"paired": True, "label": "test-machine"}
        return {}

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def headers_for(self, path: str) -> dict[str, str]:
        for call_path, headers in self.extra_headers:
            if call_path == path:
                return headers
        raise AssertionError(f"no call to {path} in {self.paths()}")

    def body(self, path: str) -> dict:
        for call_path, payload in self.calls:
            if call_path == path:
                return payload
        raise AssertionError(f"no call to {path} in {self.paths()}")


# ---- token ---------------------------------------------------------------------------------


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / ".worker-token"

    def test_generates_and_persists_a_token(self) -> None:
        first = worker.load_or_create_token(self.path)
        self.assertTrue(first)
        self.assertEqual(worker.load_or_create_token(self.path), first)

    def test_token_file_is_not_world_readable(self) -> None:
        """It is this machine's identity — anyone holding it can publish results as this worker."""
        worker.load_or_create_token(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_regenerates_when_the_file_is_empty(self) -> None:
        self.path.write_text("   \n", encoding="utf-8")
        self.assertTrue(worker.load_or_create_token(self.path).strip())

    def test_token_path_is_read_at_call_time(self) -> None:
        """Binding TOKEN_PATH as an argument default made the suite write a real token into the
        repo, because a default is evaluated once at import and patching the module missed it."""
        with mock.patch.object(worker, "TOKEN_PATH", self.path):
            worker.load_or_create_token()
        self.assertTrue(self.path.is_file())


# ---- configuration -------------------------------------------------------------------------


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / ".env"

    def test_parses_key_value_lines_ignoring_comments_and_blanks(self) -> None:
        self.path.write_text("# a comment\n\nCARVANA_WEB_URL=https://x.test\nA='q'\nB=\"r\"\n")
        values = worker.load_env_file(self.path)
        self.assertEqual(values["CARVANA_WEB_URL"], "https://x.test")
        self.assertEqual(values["A"], "q")
        self.assertEqual(values["B"], "r")

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(worker.load_env_file(Path(self.dir.name) / "nope"), {})

    def test_the_real_environment_wins_over_the_file(self) -> None:
        """So `CARVANA_WEB_URL=… python3 -m …` can override a stale bundled file."""
        with mock.patch.dict(worker.os.environ, {"K": "from-env"}, clear=False):
            self.assertEqual(worker.setting("K", {"K": "from-file"}), "from-env")

    def test_the_file_is_used_when_the_environment_is_unset(self) -> None:
        with mock.patch.dict(worker.os.environ, {}, clear=False):
            worker.os.environ.pop("K_UNSET", None)
            self.assertEqual(worker.setting("K_UNSET", {"K_UNSET": "from-file"}), "from-file")


# ---- transport -----------------------------------------------------------------------------


class HeaderTests(unittest.TestCase):
    def test_bearer_and_bypass_headers_are_both_sent(self) -> None:
        """Without the bypass header Vercel serves the password page to the worker's own API."""
        headers = worker.Client("https://x.test", "tok", "secret")._headers()
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["x-vercel-protection-bypass"], "secret")

    def test_bypass_header_is_omitted_when_unset(self) -> None:
        self.assertNotIn("x-vercel-protection-bypass",
                         worker.Client("https://x.test", "tok")._headers())

    def test_trailing_slash_is_normalized(self) -> None:
        self.assertEqual(worker.Client("https://x.test/", "tok").base_url, "https://x.test")

    def test_the_enrollment_key_is_not_a_standing_header(self) -> None:
        """It rides one call. A standing header would send it on every poll for no reason."""
        self.assertNotIn("x-grid-enroll",
                         worker.Client("https://x.test", "tok", enroll="k")._headers())


class EnrollmentTests(unittest.TestCase):
    """Registration is the only route the shared key gates — the rest prove the worker token."""

    def test_register_presents_the_enrollment_key(self) -> None:
        client = RecordingClient()
        client.register("machine")
        self.assertEqual(client.headers_for("/api/worker/register")["x-grid-enroll"],
                         "enroll-key")

    def test_no_other_route_presents_it(self) -> None:
        client = RecordingClient(claims=[None])
        client.register("machine")
        client.claim()
        client.progress("run-1", {})
        client.complete("run-1", {}, [])
        for path in ("/api/worker/claim", "/api/worker/progress", "/api/worker/complete"):
            self.assertNotIn("x-grid-enroll", client.headers_for(path), path)

    def test_the_header_is_omitted_rather_than_sent_empty_when_unconfigured(self) -> None:
        """An empty header would read as a wrong key; absent is the honest signal."""
        client = RecordingClient()
        client.enroll = None
        client.register("machine")
        self.assertEqual(client.headers_for("/api/worker/register"), {})


# ---- options -------------------------------------------------------------------------------


class OptionsTests(unittest.TestCase):
    def test_builds_options_from_a_job_payload(self) -> None:
        options = worker.options_from_payload(
            {"make": "Toyota", "model": "4Runner", "max_price": 45000, "max_miles": 80000,
             "top_n": 5, "unattended": False})
        self.assertEqual(options.make, "Toyota")
        self.assertEqual(options.max_price, 45000.0)
        self.assertEqual(options.top_n, 5)

    def test_unknown_keys_are_refused_rather_than_ignored(self) -> None:
        """Dropping a field silently would run different criteria than the person asked for."""
        with self.assertRaises(ValueError) as caught:
            worker.options_from_payload({"make": "Toyota", "max_prcie": 45000})
        self.assertIn("max_prcie", str(caught.exception))

    def test_a_job_with_no_real_criterion_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            worker.options_from_payload({"top_n": 5})

    def test_zip_is_not_defaulted_to_the_authors_city(self) -> None:
        self.assertIsNone(worker.options_from_payload({"make": "Toyota"}).zip_code)


# ---- progress ------------------------------------------------------------------------------


class _FakeState:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_event(self, event: dict) -> None:
        self.events.append(event)

    def snapshot(self) -> dict:
        return {"events": len(self.events)}


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = RecordingClient()
        self.state = _FakeState()

    def test_throttles_ordinary_events(self) -> None:
        pusher = worker.ProgressPusher(self.client, "r1", self.state, interval=1_000)
        for index in range(5):
            pusher({"kind": "vehicle", "i": index})
        # The first call is due (last push time starts at zero), the rest are inside the window.
        self.assertEqual(self.client.paths().count("/api/worker/progress"), 1)
        self.assertEqual(len(self.state.events), 5)

    def test_a_challenge_pushes_immediately(self) -> None:
        """The operator has to be told now — waiting burns the assist timeout they have."""
        pusher = worker.ProgressPusher(self.client, "r1", self.state, interval=1_000)
        pusher({"kind": "vehicle", "i": 0})
        pusher({"kind": "challenge", "label": "2021 4Runner"})
        self.assertEqual(self.client.paths().count("/api/worker/progress"), 2)

    def test_a_failed_push_does_not_raise(self) -> None:
        """Losing an update must not abort a real browser session mid-run."""
        client = RecordingClient(fail_on={"/api/worker/progress"})
        pusher = worker.ProgressPusher(client, "r1", self.state, interval=0)
        pusher({"kind": "vehicle", "i": 0})  # must not raise


# ---- reports -------------------------------------------------------------------------------


class CollectReportsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.raw = Path(self.dir.name)

    def test_collects_both_vendors_under_either_naming_scheme(self) -> None:
        (self.raw / "VIN1.carfax.txt").write_text("carfax body", encoding="utf-8")
        (self.raw / "autocheck_VIN1.txt").write_text("autocheck body", encoding="utf-8")
        rows = worker.collect_reports(make_result({"VIN1": object()}), self.raw)
        self.assertEqual({(r["vendor"], r["body"]) for r in rows},
                         {("carfax", "carfax body"), ("autocheck", "autocheck body")})

    def test_missing_reports_are_skipped_not_emitted_empty(self) -> None:
        rows = worker.collect_reports(make_result({"VIN1": object()}), self.raw)
        self.assertEqual(rows, [])

    def test_covers_every_vin_in_the_result(self) -> None:
        for vin in ("VIN1", "VIN2"):
            (self.raw / f"{vin}.carfax.txt").write_text("body", encoding="utf-8")
        rows = worker.collect_reports(make_result({"VIN1": object(), "VIN2": object()}), self.raw)
        self.assertEqual(sorted({row["vin"] for row in rows}), ["VIN1", "VIN2"])


# ---- review degradation --------------------------------------------------------------------


class ReviewTests(unittest.TestCase):
    def test_skipped_cleanly_when_the_claude_cli_is_absent(self) -> None:
        """Most machines running a worker will not have it; that is a note, not a failure."""
        state = mock.MagicMock()
        with mock.patch.object(worker.shutil, "which", return_value=None):
            worker.maybe_review(make_result(), state)
        state.note.assert_called_once()
        self.assertIn("not installed", state.note.call_args[0][1])
        state.set_review.assert_not_called()

    def test_a_review_failure_is_recorded_not_raised(self) -> None:
        state = mock.MagicMock()
        with mock.patch.object(worker.shutil, "which", return_value="/usr/bin/claude"), \
             mock.patch.object(worker.review_mod, "select_vehicles",
                               side_effect=RuntimeError("boom")):
            worker.maybe_review(make_result(), state)
        self.assertIn("error", state.set_review.call_args[0][0])


# ---- one job -------------------------------------------------------------------------------


class RunOneTests(unittest.TestCase):
    JOB = {"id": "run-1", "options": {"make": "Toyota", "max_price": 45000}}

    def _run(self, client, execute):
        with mock.patch.object(worker.pipeline, "execute", execute), \
             mock.patch.object(worker.shutil, "which", return_value=None), \
             mock.patch.object(worker, "collect_reports", return_value=[]):
            worker.run_one(client, self.JOB)

    def test_a_successful_run_publishes_a_completion(self) -> None:
        client = RecordingClient()
        self._run(client, mock.Mock(return_value=make_result()))
        self.assertIn("/api/worker/complete", client.paths())
        self.assertIn("result", client.body("/api/worker/complete"))

    def test_a_locked_profile_publishes_the_rm_remediation(self) -> None:
        client = RecordingClient()
        self._run(client, mock.Mock(side_effect=browser.ProfileLockedError("profile is locked")))
        error = client.body("/api/worker/complete")["error"]
        self.assertEqual(error["kind"], "profile_locked")
        self.assertIn("rm -f", error["hint"])

    def test_a_changed_payload_shape_is_classified(self) -> None:
        client = RecordingClient()
        self._run(client, mock.Mock(side_effect=rsc.PayloadShapeError("no records")))
        self.assertEqual(client.body("/api/worker/complete")["error"]["kind"], "payload_shape")

    def test_an_unrecognized_job_is_failed_without_running_anything(self) -> None:
        client = RecordingClient()
        execute = mock.Mock()
        with mock.patch.object(worker.pipeline, "execute", execute):
            worker.run_one(client, {"id": "run-2", "options": {"nonsense": 1}})
        execute.assert_not_called()
        self.assertEqual(client.body("/api/worker/complete")["error"]["kind"], "invalid_options")

    def test_run_one_never_raises_when_publishing_fails(self) -> None:
        """The run already happened; losing the upload must not kill the loop."""
        client = RecordingClient(fail_on={"/api/worker/complete"})
        self._run(client, mock.Mock(return_value=make_result()))  # must not raise


# ---- the loop ------------------------------------------------------------------------------


class MainLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(
            worker.os.environ,
            {"CARVANA_WEB_URL": "https://example.test",
             "VERCEL_AUTOMATION_BYPASS_SECRET": "bypass"},
            clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.token = mock.patch.object(worker, "TOKEN_PATH",
                                       Path(self.dir.name) / ".worker-token")
        self.token.start()
        self.addCleanup(self.token.stop)

    def test_missing_base_url_exits_with_a_message(self) -> None:
        """`.env` is stubbed too — otherwise this reads the developer's real one and passes."""
        with mock.patch.dict(worker.os.environ, {"CARVANA_WEB_URL": ""}, clear=False), \
             mock.patch.object(worker, "load_env_file", return_value={}):
            self.assertEqual(worker.main(["--once"]), 2)

    def test_the_base_url_can_come_from_the_env_file_alone(self) -> None:
        """The download ships a .env, and the docs tell people to run the module directly."""
        client = RecordingClient(claims=[])
        with mock.patch.dict(worker.os.environ, {"CARVANA_WEB_URL": ""}, clear=False), \
             mock.patch.object(worker, "load_env_file",
                               return_value={"CARVANA_WEB_URL": "https://from-file.test"}), \
             mock.patch.object(worker, "Client", return_value=client) as ctor:
            self.assertEqual(worker.main(["--once"]), 0)
        self.assertEqual(ctor.call_args[0][0], "https://from-file.test")

    def test_once_with_an_empty_queue_exits_zero(self) -> None:
        client = RecordingClient(claims=[])
        with mock.patch.object(worker, "Client", return_value=client):
            self.assertEqual(worker.main(["--once"]), 0)
        self.assertIn("/api/worker/register", client.paths())

    def test_once_runs_a_single_claimed_job(self) -> None:
        client = RecordingClient(claims=[RunOneTests.JOB])
        with mock.patch.object(worker, "Client", return_value=client), \
             mock.patch.object(worker, "run_one") as run_one:
            self.assertEqual(worker.main(["--once"]), 0)
        run_one.assert_called_once()

    @staticmethod
    def _announced(info: dict) -> str:
        buffer: list[str] = []
        with mock.patch("builtins.print", lambda *a, **k: buffer.append(" ".join(map(str, a)))):
            worker._announce(info, "https://grid.example")
        return "\n".join(buffer)

    def test_startup_names_the_site_and_the_waiting_queue(self) -> None:
        """The operator has to know their machine is now the one serving the site."""
        printed = self._announced({"label": "studio-mbp", "queued": 3})
        self.assertIn("studio-mbp", printed)
        self.assertIn("https://grid.example", printed)
        self.assertIn("3 search(es) waiting", printed)

    def test_startup_says_so_when_the_queue_is_empty(self) -> None:
        self.assertIn("Queue is empty", self._announced({"label": "m", "queued": 0}))

    def test_an_unclaimed_machine_prints_a_full_clickable_link(self) -> None:
        """A link, not a code — there is nothing to read off one screen and type into another."""
        printed = self._announced(
            {"label": "m", "queued": 0, "link_path": "/link?t=abc", "link_ttl_minutes": 30})
        self.assertIn("https://grid.example/link?t=abc", printed)
        self.assertIn("30 minutes", printed)

    def test_a_claimed_machine_prints_no_link(self) -> None:
        printed = self._announced({"label": "m", "queued": 0, "claimed": True, "link_path": None})
        self.assertNotIn("/link?t=", printed)
        self.assertIn("claimed", printed)


if __name__ == "__main__":
    unittest.main()
