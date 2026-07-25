"""Parser tests. Run fully offline: no browser, no network.

Fixtures are synthetic but copied from the layout of real reports captured during recon, so the
tests are reproducible on a fresh clone. Where the real archived reports exist locally
(cache/raw/, gitignored), an extra test validates against them too.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from carvana_scraper.autocheck import parse_autocheck_text
from carvana_scraper.carfax import parse_carfax_text
from carvana_scraper.models import STATUS_PARSED

RAW_DIR = Path(__file__).resolve().parent.parent / "cache" / "raw"

# --------------------------------------------------------------------------------------
# Carfax fixtures
# --------------------------------------------------------------------------------------

CARFAX_CLEAN = """Share
Print

2016 TOYOTA 4RUNNER SR5 PREMIUM

VIN: JTEZU5JR0G5129158

Report
US $49.99

61,395 miVIN: JTEZU5JR0G5129158Vehicle Details
NO ACCIDENTS REPORTED
No Accidents or Damage Reported to CARFAX
GREAT RELIABILITY
13 Service History Records
2 Previous Owners
Types of Owners: Personal Lease, Personal
23 Detailed Records Available

Total Loss
No total loss reported to CARFAX.
\t
No Issues Reported

Structural Damage
No structural damage reported to CARFAX.
\t
No Issues Reported

Airbag Deployment
No airbag deployment reported to CARFAX.
\t
No Issues Reported

Odometer Check
No indication of an odometer rollback.
\t
No Issues Indicated

Accident / Damage
No accidents or damage reported to CARFAX.
\t
No Issues Reported

Manufacturer Recall
No open recalls reported to CARFAX.
\t
No Recalls Reported

Title History
\t
Owner 1

Damage Brands
Salvage | Junk | Rebuilt | Fire | Flood | Hail | Lemon
\t
Guaranteed
No Problem

Odometer Brands
Not Actual Mileage | Exceeds Mechanical Limits
\t
Guaranteed
No Problem

Average Repair Cost
$320 avg per year
Service history
Oil changed 10,079 mi
"""

CARFAX_ACCIDENT = """Share
Print

2025 TOYOTA TACOMA SR

VIN: 3TYKD5HN3ST028017

20,189 miVIN: 3TYKD5HN3ST028017Vehicle Details
ACCIDENT
Minor to Moderate Damage
CARFAX 1-Owner Vehicle
Personal Vehicle
8 Detailed Records Available

Accident / Damage History
Event 1

10/17/2025

Accident reported: minor to moderate damage
Vehicle towed

Total Loss
No total loss reported to CARFAX.
\t
No Issues Reported

Structural Damage
CARFAX recommends that you have this vehicle inspected by a collision repair specialist.
\t
No Issues Reported

Airbag Deployment
No airbag deployment reported to CARFAX.
\t
No Issues Reported

Odometer Check
No indication of an odometer rollback.
\t
No Issues Indicated

Accident / Damage
Accident reported: 10/17/2025.
\t
Accident Reported

Manufacturer Recall
No open recalls reported to CARFAX.
\t
No Recalls Reported

Damage Brands
Salvage | Junk | Rebuilt | Fire | Flood | Hail | Lemon
\t
Guaranteed
No Problem
"""

CARFAX_BRANDED = CARFAX_CLEAN.replace(
    "Damage Brands\nSalvage | Junk | Rebuilt | Fire | Flood | Hail | Lemon\n\t\nGuaranteed\nNo Problem",
    "Damage Brands\nSalvage | Junk | Rebuilt | Fire | Flood | Hail | Lemon\n\t\nSalvage",
)

CARFAX_UNKNOWN_VERDICT = CARFAX_CLEAN.replace(
    "Total Loss\nNo total loss reported to CARFAX.\n\t\nNo Issues Reported",
    "Total Loss\nSomething new happened here.\n\t\nZorblatt Status",
)


class TestCarfaxParser(unittest.TestCase):
    def test_clean_report_fields(self):
        report = parse_carfax_text(CARFAX_CLEAN)
        assert report.status == STATUS_PARSED
        assert report.vendor == "carfax"
        assert report.vin == "JTEZU5JR0G5129158"
        assert report.owner_count == 2
        assert report.use_types == ["personal lease", "personal"]
        assert report.service_record_count == 13
        assert report.detailed_record_count == 23
        assert report.reliability_forecast == "great"
        assert report.avg_annual_repair_cost == 320

    def test_clean_report_all_checks_false_not_none(self):
        """Explicitly-clean checks must read False, never None — None means 'unknown'."""
        report = parse_carfax_text(CARFAX_CLEAN)
        for field_name in ("total_loss", "structural_damage", "airbag_deployment",
                           "odometer_rollback", "accident_reported", "open_recalls",
                           "title_brand_problem"):
            assert getattr(report, field_name) is False, field_name
        assert report.is_complete is True
        assert report.needs_review is False

    def test_odometer_read_from_header_not_a_service_record(self):
        """Regression: the header runs mileage into the VIN ("61,395 miVIN:").

        A \\bmi\\b pattern misses it and silently matches a later service-record mileage
        (10,079 mi here) — reporting a wrong odometer with no error.
        """
        report = parse_carfax_text(CARFAX_CLEAN)
        assert report.odometer_reading == 61395

    def test_accident_report(self):
        report = parse_carfax_text(CARFAX_ACCIDENT)
        assert report.accident_reported is True
        assert report.accident_count == 1
        assert report.owner_count == 1, "CARFAX 1-Owner Vehicle phrasing"
        assert report.use_types == ["personal"]
        assert any("minor to moderate" in note for note in report.notes)

    def test_narrative_does_not_override_verdict(self):
        """Structural Damage's prose recommends an inspection while its verdict is clean.

        The verdict column is authoritative; treating the prose as a finding would invent a
        structural-damage problem that Carfax did not report.
        """
        report = parse_carfax_text(CARFAX_ACCIDENT)
        assert report.structural_damage is False

    def test_branded_title_detected(self):
        report = parse_carfax_text(CARFAX_BRANDED)
        assert report.title_brand_problem is True

    def test_unknown_verdict_is_none_and_flagged(self):
        """An unfamiliar verdict must never be silently scored as clean."""
        report = parse_carfax_text(CARFAX_UNKNOWN_VERDICT)
        assert report.total_loss is None
        assert report.unrecognized_sections
        assert report.needs_review is True
        assert report.is_complete is False


# --------------------------------------------------------------------------------------
# AutoCheck fixtures
# --------------------------------------------------------------------------------------

AUTOCHECK_CLEAN = """
 Print Report
Experian AutoCheck Report
Report run: 07/25/2026 14:56:13 EDT
2016 Toyota 4Runner Limited / SR5
VIN:
JTEZU5JR0G5129158
Vehicle Age:
10 year(s)
Last Reported Odometer:
54,817 (02/24/2025)
Vehicle Usage
Lease
AutoCheck Score
96

Similar vehicles usually range between 76 and 86

Vehicle History at a Glance

State Title Brand

Clean

Auction Brand / Issues

No Issue

Accident / Damage

No Accidents or Damage Reported

Open Recall Check

No Open Recalls

Insurance Loss / Transfer

No Issue

Odometer Check

Last reported odometer:
54,817 (02/24/2025)

No Issue

Certified Pre-Owned

No CPO Info Available

Service / Repair

10 Service Record(s) Reported

Owner 1

Owner 2
"""

AUTOCHECK_RECALL = AUTOCHECK_CLEAN.replace(
    "Open Recall Check\n\nNo Open Recalls", "Open Recall Check\n\nOpen Recall")

AUTOCHECK_SALVAGE = AUTOCHECK_CLEAN.replace(
    "State Title Brand\n\nClean", "State Title Brand\n\nSalvage")

AUTOCHECK_TOTAL_LOSS = AUTOCHECK_CLEAN.replace(
    "Insurance Loss / Transfer\n\nNo Issue",
    "Insurance Loss / Transfer\n\nTotal Loss")


class TestAutoCheckParser(unittest.TestCase):
    def test_clean_report_fields(self):
        report = parse_autocheck_text(AUTOCHECK_CLEAN)
        assert report.status == STATUS_PARSED
        assert report.vendor == "autocheck"
        assert report.vin == "JTEZU5JR0G5129158"
        assert report.autocheck_score == 96
        assert (report.autocheck_score_low, report.autocheck_score_high) == (76, 86)
        assert report.odometer_reading == 54817
        assert report.use_types == ["lease"]
        assert report.service_record_count == 10
        assert report.owner_count == 2

    def test_clean_checks_are_false(self):
        report = parse_autocheck_text(AUTOCHECK_CLEAN)
        for field_name in ("title_brand_problem", "auction_problem", "accident_reported",
                           "open_recalls", "insurance_loss", "odometer_rollback"):
            assert getattr(report, field_name) is False, field_name

    def test_structural_and_airbag_left_unknown(self):
        """AutoCheck does not report these as standalone checks.

        Leaving them None is what prevents an AutoCheck-only vehicle from ever satisfying the
        completeness rule on its own.
        """
        report = parse_autocheck_text(AUTOCHECK_CLEAN)
        assert report.structural_damage is None
        assert report.airbag_deployment is None
        assert report.is_complete is False

    def test_open_recall_detected(self):
        assert parse_autocheck_text(AUTOCHECK_RECALL).open_recalls is True

    def test_salvage_title_detected(self):
        assert parse_autocheck_text(AUTOCHECK_SALVAGE).title_brand_problem is True

    def test_insurance_total_loss_maps_to_total_loss(self):
        report = parse_autocheck_text(AUTOCHECK_TOTAL_LOSS)
        assert report.insurance_loss is True
        assert report.total_loss is True


# --------------------------------------------------------------------------------------
# Optional validation against the real archived reports
# --------------------------------------------------------------------------------------

def _real(pattern: str) -> list[Path]:
    return sorted(RAW_DIR.glob(pattern)) if RAW_DIR.exists() else []


class TestRealArchivedReports(unittest.TestCase):
    """Validates the parsers against the real reports captured during recon, when present."""

    @unittest.skipUnless(_real("carfax_*.txt"), "no archived Carfax reports locally")
    def test_real_carfax_reports_parse(self):
        for path in _real("carfax_*.txt"):
            text = path.read_text(encoding="utf-8")
            if len(text) < 1500:
                continue  # a blocked capture, not a report
            report = parse_carfax_text(text)
            assert report.owner_count is not None, path.name
            assert report.accident_reported is not None, path.name
            assert report.odometer_reading and report.odometer_reading > 100, path.name

    @unittest.skipUnless(_real("autocheck_*.txt"), "no archived AutoCheck reports locally")
    def test_real_autocheck_reports_parse(self):
        for path in _real("autocheck_*.txt"):
            report = parse_autocheck_text(path.read_text(encoding="utf-8"))
            assert report.autocheck_score is not None, path.name
            assert report.title_brand_problem is not None, path.name
            assert report.accident_reported is not None, path.name


if __name__ == "__main__":
    unittest.main()
