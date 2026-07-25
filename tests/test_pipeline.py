"""Tests for the shared four-stage pipeline.

Every I/O primitive is stubbed, so these are deterministic and need no browser or network.

The point of these tests is the **event contract**. Both front ends depend on it: the CLI prints
each event's `text` verbatim, so a change to those strings silently changes CLI output, and the app
keys its UI off `kind`. Asserting the exact strings is what lets the orchestration be refactored
again without re-running a live scrape to find out what broke.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carvana_scraper import browser, history, pipeline, report as report_mod, search, vdp
from carvana_scraper.models import (
    STATUS_BLOCKED,
    STATUS_PARSED,
    HistoryReport,
    Listing,
)

VIN_A = "JTEEU5JR6P5298535"
VIN_B = "JTEFU5JR0N5264964"


def make_listing(vin: str, **over) -> Listing:
    fields = dict(
        vin=vin, vehicle_id=12345, year=2022, make="Toyota", model="4Runner", trim="SR5",
        mileage=38102, price=39998.0, shipping_fee=1990.0, kbb_value=41000.0, msrp=52000.0,
        market_adjustment=None, stock_number=555, vdp_slug="toyota-4runner", tags=(),
    )
    fields.update(over)
    return Listing(**fields)


def complete_report(vin: str, **over) -> HistoryReport:
    """A report with all seven DECISION_FIELDS set, so it is `is_complete` and rankable."""
    fields = dict(
        vin=vin, status=STATUS_PARSED, vendor="autocheck+carfax",
        owner_count=1, accident_count=0, accident_reported=False, damage_reported=False,
        total_loss=False, structural_damage=False, airbag_deployment=False,
        odometer_rollback=False, title_brand_problem=False, service_record_count=14,
        use_types=["personal"], open_recalls=False, autocheck_score=92,
        sources=["autocheck", "carfax"],
    )
    fields.update(over)
    return HistoryReport(**fields)


def autocheck_only(vin: str) -> HistoryReport:
    """AutoCheck leaves structural_damage and airbag_deployment None, so this is never complete."""
    return HistoryReport(
        vin=vin, status=STATUS_PARSED, vendor="autocheck", owner_count=2,
        accident_reported=False, total_loss=False, odometer_rollback=False,
        title_brand_problem=False, autocheck_score=85, sources=["autocheck"],
    )


def base_stats(**over) -> dict:
    stats = {
        "pages_loaded": 2, "raw_records": 40, "duplicate_records": 3,
        "pagination_effective": True, "priced_zip": "89002", "requested_zip": "89002",
        "stopped_because": None, "unparseable_records": 1, "parsed_listings": 39,
        "filtered_out": 37, "matched_before_limit": 2, "dropped_by_limit": 0, "matched": 2,
    }
    stats.update(over)
    return stats


class _FakeContext:
    def __init__(self):
        self.pages = [object()]

    def new_page(self):
        return object()


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class PipelineHarness(unittest.TestCase):
    """Stubs every primitive the pipeline reaches for and restores them afterwards."""

    def setUp(self) -> None:
        self.listings = [make_listing(VIN_A), make_listing(VIN_B, mileage=52400, price=36510.0)]
        self.stats = base_stats()
        self.reports = {VIN_A: complete_report(VIN_A), VIN_B: autocheck_only(VIN_B)}
        self.blocked_vins: set[str] = set()
        self.connection = _FakeConnection()
        self.fetch_calls: list[tuple[str, bool]] = []

        self._saved = {
            "session": browser.session,
            "human_pause": browser.human_pause,
            "collect_listings": search.collect_listings,
            "get_or_fetch": history.get_or_fetch,
            "fetch_imperfections": vdp.fetch_imperfections,
            "summarize": vdp.summarize_imperfections,
            "write_markdown": report_mod.write_markdown,
            "connect": pipeline.connect,
            "cache_stats": pipeline.cache_stats,
        }

        @contextlib.contextmanager
        def fake_session(*a, **k):
            yield _FakeContext()

        def fake_get_or_fetch(context, page, listing, connection, want_carfax, **k):
            self.fetch_calls.append((listing.vin, want_carfax))
            if want_carfax and listing.vin in self.blocked_vins:
                # What get_or_fetch really returns when Carfax is blocked on a cache miss:
                # merge_reports([autocheck, blocked_carfax]) finds one parsed report and returns
                # it unmodified, so the caller sees vendor="autocheck", status="parsed" — NOT a
                # blocked report. Stubbing a literal blocked report here would test a shape the
                # pipeline never actually receives.
                return autocheck_only(listing.vin), False
            return self.reports[listing.vin], False

        browser.session = fake_session
        browser.human_pause = lambda *a, **k: None
        search.collect_listings = lambda page, criteria, **k: (list(self.listings),
                                                              dict(self.stats))
        history.get_or_fetch = fake_get_or_fetch
        vdp.fetch_imperfections = lambda page, stock, **k: [{"name": "scratch"}]
        vdp.summarize_imperfections = lambda imps: f"{len(imps)} item(s)"
        report_mod.write_markdown = lambda body, **k: Path("/tmp/parity/report.md")
        pipeline.connect = lambda: self.connection
        pipeline.cache_stats = lambda conn: {"parsed": 6}

    def tearDown(self) -> None:
        browser.session = self._saved["session"]
        browser.human_pause = self._saved["human_pause"]
        search.collect_listings = self._saved["collect_listings"]
        history.get_or_fetch = self._saved["get_or_fetch"]
        vdp.fetch_imperfections = self._saved["fetch_imperfections"]
        vdp.summarize_imperfections = self._saved["summarize"]
        report_mod.write_markdown = self._saved["write_markdown"]
        pipeline.connect = self._saved["connect"]
        pipeline.cache_stats = self._saved["cache_stats"]

    def run_pipeline(self, abort=None, **over):
        events: list[dict] = []
        fields = dict(make="Toyota", model="4Runner", year_min=2018, year_max=2023,
                      max_price=48000.0, max_miles=90000)
        fields.update(over)
        result = pipeline.execute(pipeline.RunOptions(**fields), emit=events.append, abort=abort)
        return result, events

    @staticmethod
    def kinds(events: list[dict]) -> list[str]:
        return [event["kind"] for event in events]

    @staticmethod
    def text_of(events: list[dict], kind: str, **match) -> str:
        for event in events:
            if event["kind"] == kind and all(event.get(k) == v for k, v in match.items()):
                return event.get("text", "")
        raise AssertionError(f"no {kind} event matching {match} in {[e['kind'] for e in events]}")


class EventSequenceTests(PipelineHarness):
    def test_full_run_emits_all_four_stages_in_order(self) -> None:
        _, events = self.run_pipeline()
        stage_numbers = [e["n"] for e in events if e["kind"] == "stage"]
        self.assertEqual(stage_numbers, [1, 2, 3, 4])

    def test_stage_banner_text_matches_the_cli_format(self) -> None:
        """These strings are the CLI's output. Changing them changes the CLI."""
        _, events = self.run_pipeline()
        self.assertEqual(
            self.text_of(events, "stage", n=1),
            "\n[1/4] searching: Toyota 4Runner · 2018-2023 · ≤$48,000 landed · ≤90,000 mi · zip 89002",
        )
        self.assertEqual(
            self.text_of(events, "stage", n=2),
            "\n[2/4] AutoCheck for 2 vehicle(s) (Carvana-hosted, no rate limit observed)",
        )
        self.assertEqual(self.text_of(events, "stage", n=4), "\n[4/4] scoring")

    def test_every_text_event_is_a_string(self) -> None:
        """The CLI passes `text` straight to print(); a non-string would corrupt output."""
        _, events = self.run_pipeline()
        for event in events:
            if "text" in event:
                self.assertIsInstance(event["text"], str, f"{event['kind']} has non-str text")

    def test_per_vehicle_events_carry_index_vin_and_label(self) -> None:
        _, events = self.run_pipeline()
        autocheck = [e for e in events if e["kind"] == "vehicle" and e["stage"] == "autocheck"]
        self.assertEqual([e["i"] for e in autocheck], [1, 2])
        self.assertEqual([e["of"] for e in autocheck], [2, 2])
        self.assertEqual([e["vin"] for e in autocheck], [VIN_A, VIN_B])
        self.assertTrue(all(e["label"] for e in autocheck))

    def test_carfax_vehicle_events_carry_the_report_url(self) -> None:
        """The app needs this URL to offer 'open the report and paste it in'."""
        _, events = self.run_pipeline()
        carfax = [e for e in events if e["kind"] == "vehicle" and e["stage"] == "carfax"]
        self.assertTrue(carfax)
        for event in carfax:
            self.assertIn("carfax.com", event["carfax_url"])
            self.assertIn(event["vin"], event["carfax_url"])

    def test_a_carfax_block_on_an_uncached_vehicle_is_counted_as_neither(self) -> None:
        """Documents a pre-existing counter bug, fixed in the follow-up commit.

        merge_reports returns the AutoCheck report unmodified when Carfax is blocked, so stage 3
        sees vendor="autocheck"/status="parsed": the "has carfax" branch misses it and the
        "is blocked" branch misses it too. `carfax_blocked` therefore under-reports.

        Invariant 7 still holds — reconciliation catches it via carfax_parsed < shortlisted — but
        the counter the operator reads is wrong, which is why the app derives its blocked list
        from report state instead of from this counter.
        """
        self.blocked_vins = {VIN_A, VIN_B}
        result, _ = self.run_pipeline()
        self.assertEqual(result.manifest.shortlisted, 2)
        self.assertEqual(result.manifest.carfax_parsed, 0)
        self.assertEqual(result.manifest.carfax_blocked, 0, "the bug: should be 2")
        self.assertTrue(result.manifest.reconciliation_problems())

    def test_vehicles_without_carfax_are_identifiable_from_report_state(self) -> None:
        """The robust signal the app uses, independent of the buggy counter."""
        self.blocked_vins = {VIN_A, VIN_B}
        result, _ = self.run_pipeline()
        for vehicle in result.scored:
            report = vehicle.history
            has_carfax = "carfax" in (report.sources or []) or report.vendor == "carfax"
            self.assertFalse(has_carfax)
            self.assertFalse(vehicle.is_rankable)

    def test_conflicts_are_emitted_per_vehicle(self) -> None:
        self.reports[VIN_A] = complete_report(
            VIN_A, conflicts=["accident_reported: autocheck=False carfax=True"])
        _, events = self.run_pipeline()
        conflicts = [e for e in events if e["kind"] == "conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["vin"], VIN_A)


class StageGatingTests(PipelineHarness):
    def test_search_only_stops_after_stage_one(self) -> None:
        result, events = self.run_pipeline(search_only=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual([e["n"] for e in events if e["kind"] == "stage"], [1])
        self.assertIn("matching listing(s)", self.text_of(events, "listings"))
        self.assertEqual(self.fetch_calls, [], "search-only must fetch no reports")

    def test_no_listings_exits_one_and_prints_the_manifest(self) -> None:
        self.listings = []
        self.stats = base_stats(matched=0, matched_before_limit=0)
        result, events = self.run_pipeline()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("No vehicles matched", self.text_of(events, "empty"))
        self.assertIn("RUN MANIFEST" if False else "matched", self.text_of(events, "manifest"))

    def test_no_history_skips_both_fetch_stages(self) -> None:
        _, events = self.run_pipeline(no_history=True)
        self.assertEqual(self.fetch_calls, [])
        self.assertIn("--no-history", self.text_of(events, "stage", n=2))

    def test_no_carfax_fetches_autocheck_only(self) -> None:
        _, events = self.run_pipeline(no_carfax=True)
        self.assertTrue(self.fetch_calls)
        self.assertTrue(all(want_carfax is False for _, want_carfax in self.fetch_calls))
        self.assertIn("--no-carfax", self.text_of(events, "stage", n=3))

    def test_max_reports_caps_the_autocheck_budget(self) -> None:
        _, events = self.run_pipeline(max_reports=1)
        autocheck_calls = [c for c in self.fetch_calls if c[1] is False]
        self.assertEqual(len(autocheck_calls), 1)
        self.assertIn("AutoCheck for 1 vehicle(s)", self.text_of(events, "stage", n=2))

    def test_top_n_caps_the_carfax_shortlist(self) -> None:
        result, _ = self.run_pipeline(top_n=1)
        carfax_calls = [c for c in self.fetch_calls if c[1] is True]
        self.assertEqual(len(carfax_calls), 1)
        self.assertEqual(result.manifest.shortlisted, 1)


class CompletenessTests(PipelineHarness):
    def test_autocheck_only_vehicle_is_never_rankable(self) -> None:
        """Invariant 1: unknown is not clean.

        parse_autocheck_text never sets structural_damage or airbag_deployment, and both are
        DECISION_FIELDS, so an AutoCheck-only car must stay out of the ranking with score None.
        """
        result, _ = self.run_pipeline()
        by_vin = {v.listing.vin: v for v in result.scored}
        self.assertFalse(by_vin[VIN_B].is_rankable)
        self.assertIsNone(by_vin[VIN_B].score)
        self.assertTrue(by_vin[VIN_A].is_rankable)
        self.assertIsNotNone(by_vin[VIN_A].score)

    def test_carfax_attempted_flag_marks_the_shortlist(self) -> None:
        """Drives the report's split between 'blocked, re-run' and 'outside shortlist, raise -n'."""
        result, _ = self.run_pipeline()
        for vehicle in result.scored:
            self.assertEqual(vehicle.carfax_attempted,
                             vehicle.listing.vin in result.shortlist_vins)

    def test_disqualified_vehicle_is_excluded_from_ranking(self) -> None:
        self.reports[VIN_A] = complete_report(VIN_A, structural_damage=True)
        result, _ = self.run_pipeline()
        by_vin = {v.listing.vin: v for v in result.scored}
        self.assertTrue(by_vin[VIN_A].is_disqualified)
        self.assertIsNone(by_vin[VIN_A].score)


class WarningTests(PipelineHarness):
    def test_limit_truncation_warns_on_the_manifest_and_as_an_event(self) -> None:
        self.listings = self.listings[:1]
        self.stats = base_stats(matched=1, dropped_by_limit=1)
        result, events = self.run_pipeline(limit=1)
        warnings = [e["message"] for e in events if e["kind"] == "warning"]
        self.assertTrue(any("dropped 1 of" in w for w in warnings))
        self.assertTrue(any("dropped 1 of" in w for w in result.manifest.warnings))

    def test_priced_zip_mismatch_warns(self) -> None:
        self.stats = base_stats(priced_zip="89101")
        result, _ = self.run_pipeline()
        self.assertTrue(any("89101" in w for w in result.manifest.warnings))

    def test_missing_anchors_warn_that_scores_are_not_comparable(self) -> None:
        """Invariant 6: without both anchors, scores are not comparable across runs."""
        result, _ = self.run_pipeline(max_price=None, max_miles=None)
        self.assertFalse(result.anchor_info["anchored"])
        self.assertTrue(any("NOT comparable" in w for w in result.manifest.warnings))


class AbortTests(PipelineHarness):
    def test_abort_before_any_vehicle_still_scores_and_reports(self) -> None:
        """A cancelled run reports what it has rather than throwing the work away."""
        abort = threading.Event()
        abort.set()
        result, events = self.run_pipeline(abort=abort)
        self.assertTrue(result.aborted)
        self.assertEqual(self.fetch_calls, [])
        self.assertIn("Cancelled", self.text_of(events, "aborted"))
        self.assertEqual([e["n"] for e in events if e["kind"] == "stage"], [1, 2, 4])

    def test_abort_midway_keeps_the_vehicles_already_fetched(self) -> None:
        abort = threading.Event()
        original = history.get_or_fetch

        def abort_after_first(context, page, listing, connection, want_carfax, **k):
            result = original(context, page, listing, connection, want_carfax, **k)
            abort.set()
            return result

        history.get_or_fetch = abort_after_first
        result, _ = self.run_pipeline(abort=abort)
        self.assertTrue(result.aborted)
        self.assertEqual(len(self.fetch_calls), 1)
        self.assertEqual(len(result.histories), 1)
        self.assertEqual(len(result.scored), 1)

    def test_connection_is_closed_even_when_aborted(self) -> None:
        abort = threading.Event()
        abort.set()
        self.run_pipeline(abort=abort)
        self.assertTrue(self.connection.closed)


class RunOptionsTests(unittest.TestCase):
    def test_from_namespace_ignores_unknown_flags(self) -> None:
        """--debug and --login exist on the CLI but are not pipeline inputs."""
        args = argparse.Namespace(make="Toyota", model=None, debug=True, login=False,
                                  top_n=5, nonsense="x")
        options = pipeline.RunOptions.from_namespace(args)
        self.assertEqual(options.make, "Toyota")
        self.assertEqual(options.top_n, 5)
        self.assertFalse(hasattr(options, "debug"))

    def test_from_namespace_leaves_absent_fields_at_their_defaults(self) -> None:
        options = pipeline.RunOptions.from_namespace(argparse.Namespace(make="Toyota"))
        self.assertEqual(options.zip_code, "89002")
        self.assertEqual(options.max_pages, 8)
        self.assertEqual(options.sort, "score")

    def test_has_criterion_matches_the_cli_validation(self) -> None:
        self.assertFalse(pipeline.RunOptions().has_criterion())
        self.assertTrue(pipeline.RunOptions(make="Toyota").has_criterion())
        self.assertTrue(pipeline.RunOptions(max_miles=90000).has_criterion())
        self.assertTrue(pipeline.RunOptions(year_min=2018).has_criterion())

    def test_zip_alone_is_not_a_criterion(self) -> None:
        """Deliberate: zip_code has a default, so counting it would make every run look valid."""
        self.assertFalse(pipeline.RunOptions(zip_code="89002").has_criterion())

    def test_criteria_carries_the_scoring_anchors(self) -> None:
        options = pipeline.RunOptions(make="Toyota", max_price=48000.0, max_miles=90000)
        criteria = options.criteria()
        self.assertEqual(criteria.max_price, 48000.0)
        self.assertEqual(criteria.max_miles, 90000)


if __name__ == "__main__":
    unittest.main()
