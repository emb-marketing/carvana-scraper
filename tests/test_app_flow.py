"""End-to-end test of the app through its real HTTP server.

Exercises the whole stack — POST /api/run, poll /api/state, POST /api/ingest, POST /api/review —
with the browser, network and `claude` CLI stubbed. No Chrome launches and nothing is fetched.

This is the test that would catch the app being wired up wrongly even when every unit passes.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carvana_scraper import browser, history, pipeline, report as report_mod, search, vdp
from carvana_scraper.app import review as review_mod
from carvana_scraper.app.server import build_server
from carvana_scraper.models import STATUS_PARSED, HistoryReport, Listing

VIN_RANKED = "JTEEU5JR6P5298535"
VIN_HELD = "JTEFU5JR0N5264964"
RAW_DIR = PROJECT_ROOT / "cache" / "raw"


def make_listing(vin: str, **over) -> Listing:
    fields = dict(
        vin=vin, vehicle_id=2148592, year=2023, make="Toyota", model="4Runner", trim="SR5",
        mileage=38102, price=39998.0, shipping_fee=1990.0, kbb_value=41000.0, msrp=52000.0,
        market_adjustment=None, stock_number=555, vdp_slug="toyota-4runner", tags=(),
    )
    fields.update(over)
    return Listing(**fields)


def complete_report(vin: str) -> HistoryReport:
    return HistoryReport(
        vin=vin, status=STATUS_PARSED, vendor="autocheck+carfax", owner_count=1,
        accident_count=0, accident_reported=False, damage_reported=False, total_loss=False,
        structural_damage=False, airbag_deployment=False, odometer_rollback=False,
        title_brand_problem=False, service_record_count=14, use_types=["personal"],
        open_recalls=False, autocheck_score=92, autocheck_score_low=88, autocheck_score_high=94,
        reliability_forecast="great", avg_annual_repair_cost=320,
        sources=["autocheck", "carfax"],
    )


def autocheck_only(vin: str) -> HistoryReport:
    return HistoryReport(
        vin=vin, status=STATUS_PARSED, vendor="autocheck", owner_count=2,
        accident_reported=False, total_loss=False, odometer_rollback=False,
        title_brand_problem=False, autocheck_score=85, sources=["autocheck"],
    )


class _FakeContext:
    def __init__(self):
        self.pages = [object()]

    def new_page(self):
        return object()


class _FakeConnection:
    def close(self):
        pass


class AppFlowTests(unittest.TestCase):
    """One server, driven through its HTTP API exactly as the browser would."""

    def setUp(self) -> None:
        self.listings = [make_listing(VIN_RANKED),
                         make_listing(VIN_HELD, mileage=52400, price=36510.0, trim="TRD")]
        self.reports = {VIN_RANKED: complete_report(VIN_RANKED),
                        VIN_HELD: autocheck_only(VIN_HELD)}

        self._saved = {
            "session": browser.session, "human_pause": browser.human_pause,
            "collect": search.collect_listings, "fetch": history.get_or_fetch,
            "imps": vdp.fetch_imperfections, "write": report_mod.write_markdown,
            "connect": pipeline.connect, "stats": pipeline.cache_stats,
        }

        @contextlib.contextmanager
        def fake_session(*a, **k):
            yield _FakeContext()

        browser.session = fake_session
        browser.human_pause = lambda *a, **k: None
        search.collect_listings = lambda page, criteria, **k: (list(self.listings), {
            "pages_loaded": 1, "raw_records": 20, "parsed_listings": 20, "filtered_out": 18,
            "matched": 2, "matched_before_limit": 2, "dropped_by_limit": 0,
            "pagination_effective": True, "priced_zip": "89002", "stopped_because": None,
        })
        history.get_or_fetch = (
            lambda context, page, listing, connection, want_carfax, **k:
            (self.reports[listing.vin], False))
        vdp.fetch_imperfections = lambda page, stock, **k: []
        report_mod.write_markdown = lambda body, **k: Path("/tmp/app-flow/report.md")
        pipeline.connect = lambda *a, **k: _FakeConnection()
        pipeline.cache_stats = lambda conn: {"parsed": 2}

        self.server = build_server(port=0)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        browser.session = self._saved["session"]
        browser.human_pause = self._saved["human_pause"]
        search.collect_listings = self._saved["collect"]
        history.get_or_fetch = self._saved["fetch"]
        vdp.fetch_imperfections = self._saved["imps"]
        report_mod.write_markdown = self._saved["write"]
        pipeline.connect = self._saved["connect"]
        pipeline.cache_stats = self._saved["stats"]

    # ---- helpers ----

    def request(self, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def wait_for_status(self, wanted: str, timeout_s: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout_s
        state = {}
        while time.monotonic() < deadline:
            _, state = self.request("/api/state")
            if state.get("status") == wanted:
                return state
            time.sleep(0.05)
        self.fail(f"status never became {wanted!r}; last was {state.get('status')!r} "
                  f"error={state.get('error')}")

    def start_run(self, **over) -> dict:
        payload = {"make": "Toyota", "model": "4Runner", "max_price": 48000,
                   "max_miles": 90000, "sort": "score", "top_n": 12}
        payload.update(over)
        status, _ = self.request("/api/run", "POST", payload)
        self.assertEqual(status, 200)
        return self.wait_for_status("done")

    # ---- tests ----

    def test_run_produces_a_ranked_table_and_a_held_out_car(self) -> None:
        state = self.start_run()

        self.assertEqual([v["listing"]["vin"] for v in state["ranked"]], [VIN_RANKED])
        self.assertEqual([v["vin"] for v in state["needs_carfax"]], [VIN_HELD])
        self.assertEqual(state["criteria"],
                         "Toyota 4Runner · ≤$48,000 landed · ≤90,000 mi · zip 89002")
        self.assertTrue(state["anchored"])

    def test_ranked_rows_carry_the_computed_listing_fields(self) -> None:
        """dataclasses.asdict omits these, and they are most of what the table shows."""
        state = self.start_run()
        listing = state["ranked"][0]["listing"]
        self.assertEqual(listing["landed_price"], 41988.0)
        self.assertEqual(listing["label"], "2023 Toyota 4Runner SR5")
        self.assertIn("carfax.com", listing["carfax_url"])
        self.assertIn("carvana.com/vehicle/", listing["listing_url"])
        self.assertIsNotNone(listing["miles_per_year"])

    def test_held_out_car_offers_the_report_url_and_the_paste_remedy(self) -> None:
        state = self.start_run()
        held = state["needs_carfax"][0]
        self.assertEqual(held["remedy"], "paste_or_retry")
        self.assertIn(VIN_HELD, held["carfax_url"])
        self.assertIn("structural_damage", held["missing_decision_fields"])

    def test_a_car_outside_the_shortlist_says_to_raise_top_n_instead(self) -> None:
        state = self.start_run(top_n=1)
        remedies = {row["vin"]: row["remedy"] for row in state["needs_carfax"]}
        self.assertIn("raise_top_n", remedies.values())

    def test_serialized_history_never_leaks_the_stray_decision_fields_key(self) -> None:
        state = self.start_run()
        self.assertNotIn("DECISION_FIELDS", state["ranked"][0]["history"])

    def test_manifest_is_exposed_with_its_reconciliation_verdict(self) -> None:
        state = self.start_run()
        manifest = state["manifest"]
        self.assertEqual(manifest["counters"]["matched"], 2)
        self.assertEqual(manifest["counters"]["shortlisted"], 2)
        self.assertIn("reconciliation_problems", manifest)
        self.assertTrue(manifest["lines"])

    def test_second_run_while_one_is_active_is_refused(self) -> None:
        """The dedicated Chrome profile is single-instance, so runs must serialize."""
        slow = threading.Event()
        original = search.collect_listings
        search.collect_listings = lambda page, criteria, **k: (slow.wait(5), original(page, criteria, **k))[1]
        try:
            status, _ = self.request("/api/run", "POST", {"make": "Toyota", "max_miles": 90000})
            self.assertEqual(status, 200)
            self.wait_for_status("running")
            status, payload = self.request("/api/run", "POST",
                                           {"make": "Toyota", "max_miles": 90000})
            self.assertEqual(status, 409)
            self.assertIn("already in progress", payload["error"])
        finally:
            slow.set()
            search.collect_listings = original
        self.wait_for_status("done")

    def test_search_only_reports_listings_and_ranks_nothing(self) -> None:
        state = self.start_run(search_only=True)
        self.assertEqual(state["ranked"], [])
        self.assertTrue(any(event["kind"] == "listings" for event in state["events"]))

    def test_unanchored_run_is_flagged_as_not_comparable(self) -> None:
        """Invariant 6 only holds when both anchors are given, so the UI must be told."""
        state = self.start_run(max_price="", max_miles="", year_min=2018)
        self.assertFalse(state["anchored"])
        self.assertTrue(any("NOT comparable" in warning for warning in state["warnings"]))

    # ---- paste ----

    def test_pasting_a_real_carfax_report_moves_the_car_into_the_ranking(self) -> None:
        """The feature's whole point, driven exactly as the browser drives it."""
        capture = RAW_DIR / f"{VIN_HELD}.carfax.txt"
        if not capture.exists():
            self.skipTest(f"no archived Carfax capture for {VIN_HELD}")

        state = self.start_run()
        self.assertEqual([v["listing"]["vin"] for v in state["ranked"]], [VIN_RANKED])

        status, payload = self.request("/api/ingest", "POST", {
            "vin": VIN_HELD,
            "text": capture.read_text(encoding="utf-8", errors="replace"),
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["summary"]["vendor"], "carfax")
        self.assertTrue(payload["summary"]["has_carfax"])

        _, state = self.request("/api/state")
        vins = [v["listing"]["vin"] for v in state["ranked"]]
        disqualified = [v["listing"]["vin"] for v in state["disqualified"]]
        self.assertIn(VIN_HELD, vins + disqualified,
                      "the pasted car must land in ranked or disqualified, not stay held out")
        self.assertEqual(state["needs_carfax"], [])

    def test_paste_for_a_vin_not_in_this_run_is_refused(self) -> None:
        self.start_run()
        status, payload = self.request("/api/ingest", "POST",
                                       {"vin": "1FTFW1ET5DFC10312", "text": "x" * 3000})
        self.assertEqual(status, 422)
        self.assertIn("not one of this run's vehicles", payload["error"])

    def test_paste_of_a_challenge_page_is_refused_with_a_hint(self) -> None:
        self.start_run()
        blocked = ("captcha-delivery.com datadome " * 60) + ("Total Loss\nStructural Damage\n" * 60)
        status, payload = self.request("/api/ingest", "POST",
                                       {"vin": VIN_HELD, "text": blocked})
        self.assertEqual(status, 422)
        self.assertIn("challenge", payload["error"].lower())
        self.assertTrue(payload["hint"])

    def test_paste_of_html_is_refused(self) -> None:
        self.start_run()
        html = "<!doctype html><html>" + ("<div>Total Loss</div>" * 300) + "</html>"
        status, payload = self.request("/api/ingest", "POST", {"vin": VIN_HELD, "text": html})
        self.assertEqual(status, 422)
        self.assertIn("HTML", payload["error"])

    # ---- review ----

    def test_review_returns_findings_and_never_alters_the_ranking(self) -> None:
        state = self.start_run()
        before = [(v["listing"]["vin"], v["score"]) for v in state["ranked"]]

        reply = json.dumps({
            "pick_vin": VIN_RANKED,
            "pick_reason": "One owner, dealer-serviced throughout.",
            "findings": [{"vin": VIN_RANKED, "severity": "good",
                          "claim": "Serviced at every interval",
                          "evidence": "Regular Oil Changes"}],
            "conflict_resolutions": [],
        })
        saved = subprocess.run
        subprocess.run = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"is_error": False, "result": reply}), "")
        try:
            status, _ = self.request("/api/review", "POST", {"model": "sonnet"})
            self.assertEqual(status, 200)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, state = self.request("/api/state")
                if state.get("review") is not None:
                    break
                time.sleep(0.05)
        finally:
            subprocess.run = saved

        review = state["review"]
        self.assertIsNotNone(review, "review never completed")
        self.assertEqual(review["pick_vin"], VIN_RANKED)
        self.assertEqual(len(review["findings"]), 1)
        self.assertEqual([(v["listing"]["vin"], v["score"]) for v in state["ranked"]], before,
                         "the deterministic ranking must be byte-identical after a review")

    def test_review_smuggling_an_unknown_vin_has_it_dropped(self) -> None:
        self.start_run()
        reply = json.dumps({
            "pick_vin": "1FTFW1ET5DFC10312",
            "findings": [{"vin": "1FTFW1ET5DFC10312", "severity": "warn",
                          "claim": "invented car", "evidence": "nope"}],
            "ranking": ["1FTFW1ET5DFC10312"],
        })
        saved = subprocess.run
        subprocess.run = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"is_error": False, "result": reply}), "")
        try:
            self.request("/api/review", "POST", {"model": "sonnet"})
            deadline = time.monotonic() + 10
            state = {}
            while time.monotonic() < deadline:
                _, state = self.request("/api/state")
                if state.get("review") is not None:
                    break
                time.sleep(0.05)
        finally:
            subprocess.run = saved

        review = state["review"]
        self.assertIsNone(review["pick_vin"])
        self.assertEqual(review["findings"], [])
        self.assertTrue(review["dropped"])

    def test_review_failure_is_reported_without_touching_the_ranking(self) -> None:
        state = self.start_run()
        before = [v["listing"]["vin"] for v in state["ranked"]]
        saved = subprocess.run
        subprocess.run = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "claude blew up")
        try:
            self.request("/api/review", "POST", {"model": "sonnet"})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, state = self.request("/api/state")
                if state.get("review") is not None:
                    break
                time.sleep(0.05)
        finally:
            subprocess.run = saved

        self.assertIn("error", state["review"])
        self.assertEqual([v["listing"]["vin"] for v in state["ranked"]], before)

    # ---- failure states ----

    def test_locked_profile_becomes_a_ui_state_with_its_remediation(self) -> None:
        """A locked profile is actionable, so it must not surface as a traceback."""
        def locked(*a, **k):
            raise browser.ProfileLockedError("profile in use\n  rm -f .browser-profile/Singleton*")

        browser.session = locked
        self.request("/api/run", "POST", {"make": "Toyota", "max_miles": 90000})
        state = self.wait_for_status("error")
        self.assertEqual(state["error"]["kind"], "profile_locked")
        self.assertIn("rm -f", state["error"]["message"])
        self.assertTrue(state["error"]["hint"])

    def test_missing_scoring_config_becomes_a_ui_state(self) -> None:
        from carvana_scraper import scoring
        saved = scoring.load_config
        scoring.load_config = lambda *a, **k: (_ for _ in ()).throw(
            FileNotFoundError("config/scoring.json is required"))
        try:
            self.request("/api/run", "POST", {"make": "Toyota", "max_miles": 90000})
            state = self.wait_for_status("error")
        finally:
            scoring.load_config = saved
        self.assertEqual(state["error"]["kind"], "missing_config")

    def test_cancel_stops_the_run_and_still_reports(self) -> None:
        state = None
        gate = threading.Event()
        original = history.get_or_fetch

        def gated(context, page, listing, connection, want_carfax, **k):
            gate.set()
            time.sleep(0.2)
            return original(context, page, listing, connection, want_carfax, **k)

        history.get_or_fetch = gated
        try:
            self.request("/api/run", "POST", {"make": "Toyota", "max_miles": 90000})
            gate.wait(5)
            status, payload = self.request("/api/cancel", "POST", {})
            self.assertEqual(status, 200)
            self.assertTrue(payload["cancelling"])
            self.assertIn("after the current vehicle", payload["note"])
            state = self.wait_for_status("done")
        finally:
            history.get_or_fetch = original

        self.assertTrue(state["aborted"])
        self.assertIsNotNone(state["manifest"], "a cancelled run still reports what it gathered")


if __name__ == "__main__":
    unittest.main()
