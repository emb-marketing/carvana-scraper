"""Tests for the local-Claude report reviewer.

`subprocess.run` is stubbed throughout — no test invokes the real `claude` CLI. The guardrail tests
are the important ones: they prove the limits are enforced in code, not merely requested in the
prompt.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carvana_scraper.app import review
from carvana_scraper.models import STATUS_PARSED, HistoryReport, Listing, ScoredVehicle

VIN_A = "JTEEU5JR6P5298535"
VIN_B = "JTEFU5JR0N5264964"
VIN_OUTSIDE = "1FTFW1ET5DFC10312"


def make_vehicle(vin: str, score: float = 87.0, **history_over) -> ScoredVehicle:
    listing = Listing(
        vin=vin, vehicle_id=2148592, year=2023, make="Toyota", model="4Runner", trim="SR5",
        mileage=38102, price=39998.0, shipping_fee=1990.0, kbb_value=41000.0, msrp=52000.0,
        market_adjustment=None, stock_number=555, vdp_slug="toyota-4runner", tags=(),
    )
    fields = dict(
        vin=vin, status=STATUS_PARSED, vendor="autocheck+carfax", owner_count=1,
        accident_count=0, accident_reported=False, damage_reported=False, total_loss=False,
        structural_damage=False, airbag_deployment=False, odometer_rollback=False,
        title_brand_problem=False, autocheck_score=92, use_types=["personal"],
        sources=["autocheck", "carfax"],
    )
    fields.update(history_over)
    vehicle = ScoredVehicle(listing=listing, history=HistoryReport(**fields), score=score)
    vehicle.positives = ["1 owner"]
    vehicle.negatives = []
    return vehicle


class StubbedRun:
    """Context manager that replaces subprocess.run for the duration of a test."""

    def __init__(self, result_text: str = "", returncode: int = 0, is_error: bool = False,
                 raise_exc: Exception | None = None, envelope: str | None = None):
        self.result_text = result_text
        self.returncode = returncode
        self.is_error = is_error
        self.raise_exc = raise_exc
        self.envelope = envelope
        self.captured: dict = {}

    def __enter__(self):
        self._saved = subprocess.run

        def fake_run(command, **kwargs):
            self.captured = {"command": command, **kwargs}
            if self.raise_exc is not None:
                raise self.raise_exc
            stdout = self.envelope if self.envelope is not None else json.dumps({
                "type": "result", "is_error": self.is_error, "result": self.result_text,
            })
            return subprocess.CompletedProcess(command, self.returncode, stdout, "")

        subprocess.run = fake_run
        return self

    def __exit__(self, *exc_info):
        subprocess.run = self._saved
        return False


GOOD_REPLY = json.dumps({
    "pick_vin": VIN_B,
    "pick_reason": "Single owner and complete service history.",
    "findings": [
        {"vin": VIN_A, "severity": "warn", "claim": "Was a rental for two years",
         "evidence": "Vehicle Usage\nRental"},
        {"vin": VIN_B, "severity": "good", "claim": "Serviced at every interval",
         "evidence": "Service History\n14 records"},
    ],
    "conflict_resolutions": [
        {"vin": VIN_A, "field": "accident_reported", "resolution": "believe Carfax",
         "reasoning": "Carfax records a tow; AutoCheck says none."},
    ],
})


class DossierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_dir = Path(PROJECT_ROOT / "cache" / "raw")

    def test_dossier_names_every_vehicle_and_its_vin(self) -> None:
        dossier = review.build_dossier([make_vehicle(VIN_A), make_vehicle(VIN_B)], self.raw_dir)
        self.assertIn(VIN_A, dossier)
        self.assertIn(VIN_B, dossier)
        self.assertIn("VEHICLE 1", dossier)
        self.assertIn("VEHICLE 2", dossier)

    def test_dossier_tells_the_reviewer_not_to_reorder_or_score(self) -> None:
        """The prompt half of the guardrail; validate_response is the enforcing half."""
        dossier = review.build_dossier([make_vehicle(VIN_A)], self.raw_dir)
        collapsed = " ".join(dossier.split())
        self.assertIn("Do NOT restate those or argue about the ordering", collapsed)
        self.assertIn("A finding about any other VIN is discarded", collapsed)
        self.assertIn("do not reorder", review.REVIEWER_ROLE.lower())

    def test_dossier_surfaces_vendor_conflicts_for_resolution(self) -> None:
        vehicle = make_vehicle(VIN_A, conflicts=["accident_reported: autocheck=False carfax=True"])
        dossier = review.build_dossier([vehicle], self.raw_dir)
        self.assertIn("VENDOR DISAGREEMENTS", dossier)
        self.assertIn("accident_reported", dossier)

    def test_missing_archived_report_is_stated_not_silently_omitted(self) -> None:
        dossier = review.build_dossier([make_vehicle(VIN_OUTSIDE)], Path("/nonexistent"))
        self.assertIn("(not archived)", dossier)

    def test_archived_report_text_is_included_when_present(self) -> None:
        if not (self.raw_dir / f"{VIN_A}.carfax.txt").exists():
            self.skipTest("no archived carfax report for the fixture VIN")
        dossier = review.build_dossier([make_vehicle(VIN_A)], self.raw_dir)
        self.assertIn("CARFAX REPORT TEXT", dossier)
        self.assertNotIn("(not archived)", dossier.split("AUTOCHECK REPORT TEXT")[0])


class SelectionTests(unittest.TestCase):
    def test_only_rankable_vehicles_are_reviewed(self) -> None:
        """Disqualified and incomplete cars never reach the reviewer, so it cannot revive one."""
        ok = make_vehicle(VIN_A)
        incomplete = make_vehicle(VIN_B, structural_damage=None, airbag_deployment=None)
        incomplete.score = None
        disqualified = make_vehicle(VIN_OUTSIDE, structural_damage=True)
        disqualified.disqualifiers = ["structural damage"]

        selected = review.select_vehicles([ok, incomplete, disqualified], lambda v: 0)
        self.assertEqual([v.listing.vin for v in selected], [VIN_A])

    def test_selection_is_capped(self) -> None:
        vehicles = [make_vehicle(f"VIN{index:014d}", score=90 - index) for index in range(9)]
        selected = review.select_vehicles(vehicles, lambda v: -(v.score or 0), count=5)
        self.assertEqual(len(selected), 5)


class ResponseParsingTests(unittest.TestCase):
    def test_plain_json_parses(self) -> None:
        self.assertEqual(review._extract_json_object('{"pick_vin": "X"}'), {"pick_vin": "X"})

    def test_code_fenced_json_parses(self) -> None:
        fenced = '```json\n{"pick_vin": "X"}\n```'
        self.assertEqual(review._extract_json_object(fenced), {"pick_vin": "X"})

    def test_json_with_surrounding_prose_parses(self) -> None:
        noisy = 'Here you go:\n{"pick_vin": "X"}\nHope that helps!'
        self.assertEqual(review._extract_json_object(noisy), {"pick_vin": "X"})

    def test_no_json_raises(self) -> None:
        with self.assertRaises(review.ReviewError):
            review._extract_json_object("I could not analyse those reports.")

    def test_json_array_is_rejected(self) -> None:
        with self.assertRaises(review.ReviewError):
            review._extract_json_object("[1, 2, 3]")


class GuardrailTests(unittest.TestCase):
    """The reviewer's limits must hold in code, not just in the prompt."""

    def setUp(self) -> None:
        self.allowed = {VIN_A, VIN_B}

    def test_finding_about_an_unreviewed_vin_is_dropped(self) -> None:
        payload = {
            "pick_vin": VIN_A,
            "findings": [
                {"vin": VIN_A, "severity": "warn", "claim": "real", "evidence": "q"},
                {"vin": VIN_OUTSIDE, "severity": "warn", "claim": "smuggled", "evidence": "q"},
            ],
        }
        cleaned = review.validate_response(payload, self.allowed)
        self.assertEqual([f["vin"] for f in cleaned["findings"]], [VIN_A])
        self.assertTrue(any(VIN_OUTSIDE in note for note in cleaned["dropped"]))

    def test_pick_of_an_unreviewed_vin_is_dropped(self) -> None:
        cleaned = review.validate_response({"pick_vin": VIN_OUTSIDE}, self.allowed)
        self.assertIsNone(cleaned["pick_vin"])
        self.assertTrue(cleaned["dropped"])

    def test_emitted_ranking_or_scores_are_discarded(self) -> None:
        """Invariant 6: the deterministic ranking is the only ranking."""
        payload = {"pick_vin": VIN_A, "ranking": [VIN_B, VIN_A], "scores": {VIN_A: 99}}
        cleaned = review.validate_response(payload, self.allowed)
        self.assertNotIn("ranking", cleaned)
        self.assertNotIn("scores", cleaned)
        self.assertTrue(any("authoritative" in note for note in cleaned["dropped"]))

    def test_unknown_severity_falls_back_to_info(self) -> None:
        payload = {"findings": [{"vin": VIN_A, "severity": "CATASTROPHIC",
                                 "claim": "c", "evidence": "e"}]}
        cleaned = review.validate_response(payload, self.allowed)
        self.assertEqual(cleaned["findings"][0]["severity"], "info")

    def test_finding_without_a_claim_is_dropped(self) -> None:
        payload = {"findings": [{"vin": VIN_A, "claim": "   ", "evidence": "e"}]}
        self.assertEqual(review.validate_response(payload, self.allowed)["findings"], [])

    def test_conflict_resolution_about_an_unreviewed_vin_is_dropped(self) -> None:
        payload = {"conflict_resolutions": [
            {"vin": VIN_OUTSIDE, "field": "accident_reported", "resolution": "r", "reasoning": "w"},
        ]}
        cleaned = review.validate_response(payload, self.allowed)
        self.assertEqual(cleaned["conflict_resolutions"], [])

    def test_vins_are_matched_case_insensitively(self) -> None:
        payload = {"pick_vin": VIN_A.lower(),
                   "findings": [{"vin": VIN_A.lower(), "claim": "c", "evidence": "e"}]}
        cleaned = review.validate_response(payload, self.allowed)
        self.assertEqual(cleaned["pick_vin"], VIN_A)
        self.assertEqual(cleaned["findings"][0]["vin"], VIN_A)

    def test_garbage_shapes_do_not_raise(self) -> None:
        payload = {"findings": ["not a dict", 42, None], "conflict_resolutions": "nope"}
        cleaned = review.validate_response(payload, self.allowed)
        self.assertEqual(cleaned["findings"], [])
        self.assertEqual(cleaned["conflict_resolutions"], [])


class InvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vehicles = [make_vehicle(VIN_A), make_vehicle(VIN_B, score=81.0)]

    def test_successful_review_returns_validated_commentary(self) -> None:
        with StubbedRun(result_text=GOOD_REPLY):
            result = review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        self.assertEqual(result["pick_vin"], VIN_B)
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(len(result["conflict_resolutions"]), 1)
        self.assertEqual(result["dropped"], [])
        self.assertEqual(result["model"], review.DEFAULT_MODEL)

    def test_invocation_is_toolless_and_mcp_free(self) -> None:
        """These exact flags were verified against claude 2.1.163."""
        with StubbedRun(result_text=GOOD_REPLY) as stub:
            review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        command = stub.captured["command"]
        self.assertEqual(command[:2], ["claude", "-p"])
        self.assertIn("--strict-mcp-config", command)
        self.assertIn('{"mcpServers":{}}', command)
        self.assertEqual(command[command.index("--allowedTools") + 1], "")

    def test_runs_outside_the_repo_so_project_context_is_not_injected(self) -> None:
        """Verified empirically: from inside the repo the project CLAUDE.md reaches the model."""
        with StubbedRun(result_text=GOOD_REPLY) as stub:
            review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        cwd = Path(stub.captured["cwd"]).resolve()
        self.assertFalse(str(cwd).startswith(str(PROJECT_ROOT.resolve())))

    def test_dossier_is_passed_on_stdin(self) -> None:
        with StubbedRun(result_text=GOOD_REPLY) as stub:
            review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        self.assertIn(VIN_A, stub.captured["input"])

    def test_haiku_is_refused(self) -> None:
        with self.assertRaises(review.ReviewError):
            review.run_review(self.vehicles, model="haiku")

    def test_empty_vehicle_list_is_refused(self) -> None:
        with self.assertRaises(review.ReviewError) as caught:
            review.run_review([])
        self.assertIn("no vehicles", str(caught.exception))

    def test_timeout_raises_review_error(self) -> None:
        with StubbedRun(raise_exc=subprocess.TimeoutExpired("claude", 300)):
            with self.assertRaises(review.ReviewError) as caught:
                review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        self.assertIn("timed out", str(caught.exception))

    def test_missing_cli_raises_review_error(self) -> None:
        with StubbedRun(raise_exc=FileNotFoundError()):
            with self.assertRaises(review.ReviewError) as caught:
                review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        self.assertIn("not on PATH", str(caught.exception))

    def test_nonzero_exit_raises_review_error(self) -> None:
        with StubbedRun(returncode=1, envelope="boom"):
            with self.assertRaises(review.ReviewError) as caught:
                review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))
        self.assertIn("exited 1", str(caught.exception))

    def test_error_envelope_raises_review_error(self) -> None:
        with StubbedRun(result_text="rate limited", is_error=True):
            with self.assertRaises(review.ReviewError):
                review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))

    def test_unparseable_reply_raises_review_error(self) -> None:
        with StubbedRun(result_text="I cannot help with that."):
            with self.assertRaises(review.ReviewError):
                review.run_review(self.vehicles, raw_dir=Path("/nonexistent"))

    def test_codex_backend_explains_why_it_is_unavailable(self) -> None:
        with self.assertRaises(review.ReviewError) as caught:
            review.run_review(self.vehicles, backend="codex", raw_dir=Path("/nonexistent"))
        self.assertIn("ENOENT", str(caught.exception))

    def test_unknown_backend_is_refused(self) -> None:
        with self.assertRaises(review.ReviewError):
            review.run_review(self.vehicles, backend="gpt5", raw_dir=Path("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
