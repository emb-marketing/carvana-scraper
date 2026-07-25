"""Report and manifest tests.

The manifest exists to defend against this tool's worst failure mode: printing a confident table
of a few cars when many matched and most were silently lost upstream. These tests pin that
behaviour down, including the non-zero exit code that makes a partial run visibly partial.
"""

from __future__ import annotations

import unittest

from carvana_scraper.report import RunManifest, manifest_exit_code, render
from carvana_scraper.scoring import load_config, score_vehicle
from carvana_scraper.models import STATUS_BLOCKED, HistoryReport, SearchCriteria

from .test_scoring import make_complete_history, make_listing

CONFIG = load_config()
CRITERIA = SearchCriteria(make="Toyota", model="4Runner", max_price=50000, max_miles=100000)


def reconciled_manifest() -> RunManifest:
    return RunManifest(criteria="test", pages_loaded=1, raw_records=20, parsed_listings=20,
                       matched=10, shortlisted=3, autocheck_parsed=10,
                       carfax_attempted=3, carfax_parsed=3)


def scored(listing=None, history=None):
    return score_vehicle(listing or make_listing(), history or make_complete_history(),
                         CRITERIA, CONFIG, price_anchor=50000, miles_anchor=100000)


class TestManifestReconciliation(unittest.TestCase):
    def test_fully_reconciled_run_exits_zero(self):
        manifest = reconciled_manifest()
        assert manifest.reconciliation_problems() == []
        assert manifest_exit_code(manifest) == 0

    def test_partial_carfax_coverage_exits_nonzero(self):
        manifest = reconciled_manifest()
        manifest.carfax_parsed = 1
        manifest.carfax_blocked = 2
        problems = manifest.reconciliation_problems()
        assert problems
        assert "only 1 of 3" in problems[0]
        assert manifest_exit_code(manifest) == 1

    def test_zero_matches_is_a_problem(self):
        manifest = RunManifest(criteria="test", matched=0)
        assert any("zero vehicles matched" in problem
                   for problem in manifest.reconciliation_problems())
        assert manifest_exit_code(manifest) == 1

    def test_matches_but_no_history_is_a_problem(self):
        manifest = RunManifest(criteria="test", matched=10, autocheck_parsed=0)
        assert any("no AutoCheck report parsed" in problem
                   for problem in manifest.reconciliation_problems())

    def test_shortlist_with_no_carfax_is_a_problem(self):
        manifest = RunManifest(criteria="test", matched=10, autocheck_parsed=10,
                               shortlisted=5, carfax_parsed=0)
        assert any("main ranking will be empty" in problem
                   for problem in manifest.reconciliation_problems())


class TestRender(unittest.TestCase):
    def test_manifest_appears_in_output(self):
        body = render([scored()], CRITERIA, reconciled_manifest())
        assert "RUN MANIFEST" in body
        assert "CARVANA RANKING" in body
        assert CRITERIA.describe() in body

    def test_complete_vehicle_is_ranked(self):
        manifest = reconciled_manifest()
        body = render([scored()], CRITERIA, manifest)
        assert manifest.ranked == 1
        assert manifest.needs_carfax == 0
        assert "RANKED — 1 vehicle(s)" in body

    def test_autocheck_only_vehicle_is_held_out_not_ranked(self):
        """The strict completeness reading: no Carfax means no place in the main ranking."""
        autocheck_only = make_complete_history(structural_damage=None, airbag_deployment=None,
                                               sources=["autocheck"], vendor="autocheck")
        manifest = reconciled_manifest()
        body = render([scored(history=autocheck_only)], CRITERIA, manifest)
        assert manifest.ranked == 0
        assert manifest.needs_carfax == 1
        assert "NEEDS CARFAX" in body

    def test_blocked_vehicle_is_held_out(self):
        blocked = HistoryReport(vin="B" * 17, status=STATUS_BLOCKED, vendor="carfax")
        manifest = reconciled_manifest()
        render([scored(history=blocked)], CRITERIA, manifest)
        assert manifest.ranked == 0
        assert manifest.needs_carfax == 1

    def test_disqualified_vehicle_gets_its_own_section_with_reason(self):
        manifest = reconciled_manifest()
        body = render([scored(history=make_complete_history(title_brand_problem=True))],
                      CRITERIA, manifest)
        assert manifest.disqualified == 1
        assert manifest.ranked == 0
        assert "DISQUALIFIED" in body
        assert "branded title" in body

    def test_incomplete_run_warning_is_printed(self):
        manifest = reconciled_manifest()
        manifest.carfax_parsed = 1
        body = render([scored()], CRITERIA, manifest)
        assert "INCOMPLETE RUN" in body

    def test_vendor_conflict_is_surfaced_in_detail_block(self):
        history = make_complete_history()
        history.conflicts = ["accident_reported: carfax=True autocheck=False"]
        body = render([scored(history=history)], CRITERIA, reconciled_manifest())
        assert "vendor disagreement" in body

    def test_ranking_sorts_by_score_descending(self):
        good = scored(make_listing(vin="A" * 17, price=25000, mileage=30000))
        poor = scored(make_listing(vin="B" * 17, price=48000, mileage=95000))
        assert good.score > poor.score, "cheaper, lower-mileage, under-book car must score higher"
        body = render([poor, good], CRITERIA, reconciled_manifest())
        # Compare positions inside the table only — the detail blocks below repeat these values.
        table = body.split("TOP ")[0]
        assert table.index("$25,000") < table.index("$48,000"), \
            "higher-scoring vehicle must appear first despite being passed in second"

    def test_sort_by_price_puts_cheapest_first(self):
        expensive = scored(make_listing(vin="A" * 17, price=48000))
        cheap = scored(make_listing(vin="B" * 17, price=25000))
        body = render([expensive, cheap], CRITERIA, reconciled_manifest(), sort_by="price")
        assert body.index("$25,000") < body.index("$48,000")

    def test_empty_result_set_renders_without_crashing(self):
        body = render([], CRITERIA, RunManifest(criteria="test"))
        assert "(none)" in body

    def test_limit_truncation_is_visible_in_the_manifest(self):
        """A --limit that silently drops matches is the narrowing the manifest exists to expose."""
        manifest = reconciled_manifest()
        manifest.matched_before_limit = 13
        manifest.dropped_by_limit = 7
        manifest.matched = 6
        body = render([scored()], CRITERIA, manifest)
        assert "dropped by --limit" in body
        assert "matched criteria" in body
        rendered = "\n".join(manifest.lines())
        assert "13" in rendered and "7" in rendered

    def test_held_out_vehicles_are_split_by_remedy(self):
        """Blocked ("re-run") and never-shortlisted ("raise --top-n") need different advice."""
        autocheck_only = make_complete_history(structural_damage=None, airbag_deployment=None,
                                               sources=["autocheck"], vendor="autocheck")
        attempted = scored(make_listing(vin="A" * 17), autocheck_only)
        attempted.carfax_attempted = True
        skipped = scored(make_listing(vin="B" * 17), autocheck_only)
        skipped.carfax_attempted = False

        body = render([attempted, skipped], CRITERIA, reconciled_manifest())
        assert "Carfax was attempted but not obtained (1)" in body
        assert "blocked VINs are deliberately never cached" in body
        assert "Outside the Carfax shortlist (1)" in body
        assert "Raise --top-n" in body


if __name__ == "__main__":
    unittest.main()
