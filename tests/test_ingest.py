"""Tests for pasted-report ingestion.

Offline. The tests that need real report text use the archived captures in `cache/raw/` and skip
when that directory is empty — it is gitignored, so a fresh clone has none.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carvana_scraper import cache, scoring
from carvana_scraper.app import ingest
from carvana_scraper.app.serialize import has_carfax
from carvana_scraper.models import STATUS_PARSED, HistoryReport, Listing

RAW_DIR = PROJECT_ROOT / "cache" / "raw"


def find_capture(vendor: str) -> tuple[str, str] | None:
    """Return (vin, text) for one archived report of the given vendor, if any exists.

    Handles both naming schemes present in cache/raw/: `{VIN}.{vendor}.txt` written by
    cache.archive_raw, and `{vendor}_{VIN}.txt` left by the recon probes.
    """
    if not RAW_DIR.exists():
        return None
    for pattern, extract in ((f"*.{vendor}.txt", lambda p: p.name.split(".")[0]),
                             (f"{vendor}_*.txt", lambda p: p.stem.split("_", 1)[1])):
        for path in sorted(RAW_DIR.glob(pattern)):
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) >= ingest.MIN_REPORT_CHARS:
                return extract(path), text
    return None


def make_listing(vin: str) -> Listing:
    return Listing(
        vin=vin, vehicle_id=2148592, year=2023, make="Toyota", model="4Runner", trim="SR5",
        mileage=38102, price=39998.0, shipping_fee=1990.0, kbb_value=41000.0, msrp=52000.0,
        market_adjustment=None, stock_number=555, vdp_slug="toyota-4runner", tags=(),
    )


def autocheck_only(vin: str) -> HistoryReport:
    """An AutoCheck-only report: structural_damage / airbag_deployment stay None."""
    return HistoryReport(
        vin=vin, status=STATUS_PARSED, vendor="autocheck", owner_count=2,
        accident_reported=False, total_loss=False, odometer_rollback=False,
        title_brand_problem=False, autocheck_score=85, sources=["autocheck"],
    )


def memory_connection(test: unittest.TestCase | None = None) -> sqlite3.Connection:
    """An in-memory cache DB with the real schema, so nothing touches cache/carvana.db.

    Pass the test case to have the connection closed on teardown.
    """
    connection = cache.connect(":memory:")
    if test is not None:
        test.addCleanup(connection.close)
    return connection


class VendorDetectionTests(unittest.TestCase):
    def test_autocheck_score_identifies_autocheck(self) -> None:
        self.assertEqual(ingest.detect_vendor("...\nAutoCheck Score\n92\n..."),
                         ingest.VENDOR_AUTOCHECK)

    def test_two_carfax_section_labels_identify_carfax(self) -> None:
        text = "Total Loss\nNo total loss reported\nStructural Damage\nNo issues reported"
        self.assertEqual(ingest.detect_vendor(text), ingest.VENDOR_CARFAX)

    def test_a_single_incidental_mention_is_not_enough(self) -> None:
        self.assertIsNone(ingest.detect_vendor("I looked at the CARFAX yesterday"))

    def test_unrelated_text_detects_nothing(self) -> None:
        self.assertIsNone(ingest.detect_vendor("the quick brown fox " * 200))

    def test_real_captures_are_classified_correctly(self) -> None:
        for vendor in ("carfax", "autocheck"):
            capture = find_capture(vendor)
            if capture is None:
                self.skipTest(f"no archived {vendor} report in cache/raw/")
            _, text = capture
            self.assertEqual(ingest.detect_vendor(text), vendor)


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vin = "JTEEU5JR6P5298535"
        self.filler = "Total Loss\nStructural Damage\n" + ("report line\n" * 400)

    def test_empty_paste_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.validate("   ", self.vin)

    def test_missing_vin_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.validate(self.filler, "")

    def test_short_paste_is_refused_at_the_same_floor_the_scraper_uses(self) -> None:
        """history._fetch_report_page rejects under 1500 chars; a paste must not be laxer."""
        with self.assertRaises(ingest.IngestError) as caught:
            ingest.validate("Total Loss\nStructural Damage\n" + "x" * 100, self.vin)
        self.assertIn("characters", str(caught.exception))

    def test_datadome_challenge_text_is_refused(self) -> None:
        """Pasting a challenge shell must never be recorded as history.

        A blocked vehicle is deliberately never cached (invariant 3) precisely so it retries.
        Accepting the shell here would cache a block as fact and drop the car from every later run.
        """
        blocked = ("captcha-delivery.com geo.captcha-delivery " * 40) + self.filler
        with self.assertRaises(ingest.IngestError) as caught:
            ingest.validate(blocked, self.vin)
        self.assertIn("challenge", str(caught.exception).lower())

    def test_html_source_is_refused_rather_than_silently_mis_parsed(self) -> None:
        """HTML would not raise in the parser — it would yield an all-None report.

        That car is then correctly held out, but the operator was told the paste worked. Refusing
        up front is the honest failure.
        """
        html = "<!doctype html><html><body>" + ("<div>Total Loss</div>" * 200) + "</body></html>"
        with self.assertRaises(ingest.IngestError) as caught:
            ingest.validate(html, self.vin)
        self.assertIn("HTML", str(caught.exception))

    def test_pdf_source_is_refused(self) -> None:
        pdf = "%PDF-1.7\n" + self.filler
        with self.assertRaises(ingest.IngestError):
            ingest.validate(pdf, self.vin)

    def test_unrecognisable_text_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError) as caught:
            ingest.validate("lorem ipsum dolor " * 300, self.vin)
        self.assertIn("Carfax or an AutoCheck", str(caught.exception))

    def test_a_real_capture_validates(self) -> None:
        capture = find_capture("carfax")
        if capture is None:
            self.skipTest("no archived carfax report in cache/raw/")
        vin, text = capture
        ingest.validate(text, vin)  # must not raise


class ParsePasteTests(unittest.TestCase):
    def test_vin_is_taken_from_the_argument_not_the_text(self) -> None:
        """A mis-paste must not be filed under the car the operator actually copied.

        Both parsers self-extract a VIN when not given one, so passing it explicitly is what
        prevents a Tacoma's report being stored against a 4Runner.
        """
        capture = find_capture("carfax")
        if capture is None:
            self.skipTest("no archived carfax report in cache/raw/")
        text_vin, text = capture
        other = "1FTFW1ET5DFC10312"
        self.assertNotEqual(text_vin, other)
        report = ingest.parse_paste(text, other)
        self.assertEqual(report.vin, other)

    def test_real_carfax_capture_parses_with_decision_fields(self) -> None:
        capture = find_capture("carfax")
        if capture is None:
            self.skipTest("no archived carfax report in cache/raw/")
        vin, text = capture
        report = ingest.parse_paste(text, vin)
        self.assertTrue(report.is_parsed)
        self.assertEqual(report.vendor, "carfax")
        self.assertIsNotNone(report.structural_damage)
        self.assertIsNotNone(report.airbag_deployment)

    def test_real_autocheck_capture_parses_and_stays_incomplete(self) -> None:
        """Invariant: AutoCheck alone can never be complete, so it can never be ranked."""
        capture = find_capture("autocheck")
        if capture is None:
            self.skipTest("no archived autocheck report in cache/raw/")
        vin, text = capture
        report = ingest.parse_paste(text, vin)
        self.assertTrue(report.is_parsed)
        self.assertFalse(report.is_complete)
        self.assertIn("structural_damage", report.missing_decision_fields)
        self.assertIn("airbag_deployment", report.missing_decision_fields)


class IngestMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        capture = find_capture("carfax")
        if capture is None:
            self.skipTest("no archived carfax report in cache/raw/")
        self.vin, self.text = capture
        self.listing = make_listing(self.vin)
        self.connection = memory_connection(self)
        self.raw_dir = RAW_DIR

    def test_pasting_carfax_completes_an_autocheck_only_vehicle(self) -> None:
        """The whole point of the feature: a blocked car becomes rankable from a paste."""
        existing = autocheck_only(self.vin)
        self.assertFalse(existing.is_complete)
        self.assertFalse(has_carfax(existing))

        summary = ingest.ingest(self.text, self.listing, existing, self.connection)

        self.assertEqual(summary["vendor"], "carfax")
        self.assertTrue(summary["has_carfax"])
        self.assertTrue(summary["is_complete"])
        self.assertEqual(summary["missing_decision_fields"], [])

    def test_merge_is_autocheck_first_so_it_matches_a_scraped_run(self) -> None:
        """_FIRST_WINS_FIELDS and source_url resolve by order, so order is load-bearing."""
        existing = autocheck_only(self.vin)
        existing.autocheck_score = 85
        summary = ingest.ingest(self.text, self.listing, existing, self.connection)
        merged = summary["report"]
        self.assertEqual(merged.vendor, "autocheck+carfax")
        self.assertEqual(sorted(merged.sources), ["autocheck", "carfax"])
        self.assertEqual(merged.autocheck_score, 85, "AutoCheck-only field must survive the merge")

    def test_merge_is_pessimistic_about_problems(self) -> None:
        """Invariant 2: if either vendor reports a problem, the vehicle has it."""
        existing = autocheck_only(self.vin)
        existing.accident_reported = True
        summary = ingest.ingest(self.text, self.listing, existing, self.connection)
        self.assertTrue(summary["report"].accident_reported)

    def test_disagreement_is_recorded_as_a_conflict(self) -> None:
        existing = autocheck_only(self.vin)
        existing.accident_reported = True
        parsed = ingest.parse_paste(self.text, self.vin)
        if parsed.accident_reported is not False:
            self.skipTest("archived report also reports an accident; no disagreement to test")
        summary = ingest.ingest(self.text, self.listing, existing, self.connection)
        self.assertTrue(any("accident_reported" in c for c in summary["conflicts"]))

    def test_result_is_written_to_the_cache(self) -> None:
        ingest.ingest(self.text, self.listing, autocheck_only(self.vin), self.connection)
        row = cache.get_history(self.connection, self.vin)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], STATUS_PARSED)
        self.assertIn("carfax", row["vendor"])

    def test_paste_with_no_existing_report_is_accepted_alone(self) -> None:
        summary = ingest.ingest(self.text, self.listing, None, self.connection)
        self.assertEqual(summary["vendor"], "carfax")
        self.assertTrue(summary["has_carfax"])

    def test_a_blocked_existing_report_is_not_merged_into(self) -> None:
        """A blocked placeholder carries no findings, so merging it would add nothing."""
        blocked = HistoryReport(vin=self.vin, status="history_blocked", vendor="carfax")
        summary = ingest.ingest(self.text, self.listing, blocked, self.connection)
        self.assertTrue(summary["has_carfax"])
        self.assertEqual(summary["merged_vendor"], "carfax")


class RescoreTests(unittest.TestCase):
    """A completed paste must move the car into the ranking without disturbing the others."""

    def setUp(self) -> None:
        capture = find_capture("carfax")
        if capture is None:
            self.skipTest("no archived carfax report in cache/raw/")
        self.vin, self.text = capture
        self.config = scoring.load_config()

    def test_rescore_makes_the_pasted_vehicle_rankable(self) -> None:
        from carvana_scraper.pipeline import RunOptions, RunResult
        from carvana_scraper.report import RunManifest

        listing = make_listing(self.vin)
        options = RunOptions(make="Toyota", model="4Runner", max_price=48000.0, max_miles=90000)
        result = RunResult(options=options, criteria=options.criteria(),
                           manifest=RunManifest(), config=self.config)
        result.listings = [listing]
        result.histories = {self.vin: autocheck_only(self.vin)}
        result.shortlist_vins = {self.vin}

        before, _ = ingest.rescore(result, self.config)
        self.assertFalse(before[0].is_rankable)
        self.assertIsNone(before[0].score)

        merged = ingest.ingest(self.text, listing, result.histories[self.vin],
                               memory_connection(self))["report"]
        result.histories[self.vin] = merged

        after, anchor = ingest.rescore(result, self.config)
        self.assertTrue(anchor["anchored"])
        if merged.is_complete and not after[0].is_disqualified:
            self.assertTrue(after[0].is_rankable)
            self.assertIsNotNone(after[0].score)


if __name__ == "__main__":
    unittest.main()
