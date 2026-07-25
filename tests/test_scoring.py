"""Scoring and merge tests. Pure logic — no browser, no network.

The load-bearing invariants under test:
  * every hard disqualifier excludes a vehicle
  * a vehicle whose history could not be read is never scored as clean
  * an identical vehicle scores identically across runs with different candidate sets
  * a missing signal is renormalized away, not treated as a bad value
  * the two vendors merge pessimistically, and disagreements are recorded
"""

from __future__ import annotations

import unittest

from carvana_scraper.history import merge_reports
from carvana_scraper.models import (
    STATUS_BLOCKED, STATUS_PARSED, HistoryReport, Listing, SearchCriteria,
)
from carvana_scraper.scoring import find_disqualifiers, load_config, score_all, score_vehicle

CONFIG = load_config()

CRITERIA = SearchCriteria(make="Toyota", model="4Runner", max_price=50000, max_miles=100000)


def make_listing(
    vin: str = "JTEZU5JR0G5129158",
    price: float = 30000,
    mileage: int = 50000,
    year: int = 2019,
    shipping: float = 0,
    kbb: float | None = 30000,
) -> Listing:
    return Listing(
        vin=vin, vehicle_id=1, year=year, make="Toyota", model="4Runner", trim="SR5",
        mileage=mileage, price=price, shipping_fee=shipping, kbb_value=kbb,
        msrp=45000, market_adjustment=-5000, stock_number=123, vdp_slug="x",
    )


def make_complete_history(**overrides) -> HistoryReport:
    """A fully-known, entirely clean history — the baseline a scored vehicle needs."""
    fields = dict(
        vin="JTEZU5JR0G5129158", status=STATUS_PARSED, vendor="carfax+autocheck",
        sources=["autocheck", "carfax"],
        owner_count=1, accident_count=0, accident_reported=False, damage_reported=False,
        total_loss=False, structural_damage=False, airbag_deployment=False,
        odometer_rollback=False, title_brand_problem=False, open_recalls=False,
        auction_problem=False, insurance_loss=False,
        service_record_count=12, use_types=["personal"], autocheck_score=95,
        autocheck_score_low=88, autocheck_score_high=93, reliability_forecast="great",
        avg_annual_repair_cost=300,
    )
    fields.update(overrides)
    return HistoryReport(**fields)


class TestDisqualifiers(unittest.TestCase):
    def test_each_enabled_disqualifier_fires(self):
        for field_name in ("title_brand_problem", "odometer_rollback", "structural_damage",
                           "airbag_deployment", "total_loss", "insurance_loss"):
            history = make_complete_history(**{field_name: True})
            reasons = find_disqualifiers(history, CONFIG)
            assert reasons, f"{field_name} did not disqualify"

    def test_clean_history_is_not_disqualified(self):
        assert find_disqualifiers(make_complete_history(), CONFIG) == []

    def test_unknown_field_does_not_disqualify(self):
        """None means unknown. Unknown is handled by the completeness rule, not by exclusion."""
        history = make_complete_history(structural_damage=None)
        assert find_disqualifiers(history, CONFIG) == []

    def test_disqualified_vehicle_is_not_scored(self):
        scored = score_vehicle(make_listing(), make_complete_history(total_loss=True),
                               CRITERIA, CONFIG, price_anchor=50000, miles_anchor=100000)
        assert scored.is_disqualified
        assert scored.score is None
        assert scored.is_rankable is False


class TestCompletenessRule(unittest.TestCase):
    def test_blocked_history_is_never_scored(self):
        history = HistoryReport(vin="X", status=STATUS_BLOCKED, vendor="carfax")
        scored = score_vehicle(make_listing(), history, CRITERIA, CONFIG,
                               price_anchor=50000, miles_anchor=100000)
        assert scored.score is None
        assert scored.is_rankable is False

    def test_autocheck_only_vehicle_is_not_rankable(self):
        """AutoCheck cannot establish structural damage or airbag deployment on its own."""
        history = make_complete_history(structural_damage=None, airbag_deployment=None,
                                        sources=["autocheck"], vendor="autocheck")
        scored = score_vehicle(make_listing(), history, CRITERIA, CONFIG,
                               price_anchor=50000, miles_anchor=100000)
        assert scored.is_rankable is False
        assert scored.score is None
        assert scored.completeness_marker == "!"

    def test_missing_accident_data_is_not_treated_as_no_accident(self):
        unknown = make_complete_history(accident_reported=None, accident_count=None)
        known_clean = make_complete_history()
        assert "accidents" not in score_vehicle(
            make_listing(), unknown, CRITERIA, CONFIG, 50000, 100000).components
        assert "accidents" in score_vehicle(
            make_listing(), known_clean, CRITERIA, CONFIG, 50000, 100000).components


class TestScoreStability(unittest.TestCase):
    def test_same_vehicle_scores_the_same_across_different_candidate_sets(self):
        """The whole reason scoring anchors to criteria instead of the observed min/max.

        Min-max normalizing inside the result set would make this vehicle's score move purely
        because the surrounding inventory changed.
        """
        target = make_listing(vin="AAAAAAAAAAAAAAAAA", price=30000, mileage=50000)
        history = make_complete_history(vin="AAAAAAAAAAAAAAAAA")

        cheap_field = [(target, history, None),
                       (make_listing(vin="B" * 17, price=12000, mileage=20000),
                        make_complete_history(vin="B" * 17), None)]
        pricey_field = [(target, history, None),
                        (make_listing(vin="C" * 17, price=49000, mileage=95000),
                         make_complete_history(vin="C" * 17), None)]

        first, _ = score_all(cheap_field, CRITERIA, CONFIG)
        second, _ = score_all(pricey_field, CRITERIA, CONFIG)
        assert first[0].score == second[0].score

    def test_missing_anchor_is_flagged_as_unanchored(self):
        loose = SearchCriteria(make="Toyota")
        _, info = score_all([(make_listing(), make_complete_history(), None)], loose, CONFIG)
        assert info["anchored"] is False
        assert "NOT comparable across runs" in info["note"]

    def test_present_anchors_report_as_anchored(self):
        _, info = score_all([(make_listing(), make_complete_history(), None)], CRITERIA, CONFIG)
        assert info["anchored"] is True


class TestRenormalization(unittest.TestCase):
    def test_missing_kbb_does_not_penalize(self):
        """A data gap must not masquerade as a bad car."""
        with_kbb = score_vehicle(make_listing(kbb=30000), make_complete_history(),
                                 CRITERIA, CONFIG, 50000, 100000)
        without_kbb = score_vehicle(make_listing(kbb=None), make_complete_history(),
                                    CRITERIA, CONFIG, 50000, 100000)
        assert "kbb_delta" not in without_kbb.components
        # At list price exactly equal to KBB the component scores 0.5, i.e. below this vehicle's
        # overall average — so dropping it should not drag the score down.
        assert without_kbb.score >= with_kbb.score

    def test_under_book_beats_over_book(self):
        under = score_vehicle(make_listing(price=27000, kbb=30000), make_complete_history(),
                              CRITERIA, CONFIG, 50000, 100000)
        over = score_vehicle(make_listing(price=33000, kbb=30000), make_complete_history(),
                             CRITERIA, CONFIG, 50000, 100000)
        assert under.components["kbb_delta"] > over.components["kbb_delta"]
        assert under.score > over.score


class TestDerivedMetrics(unittest.TestCase):
    def test_cost_per_remaining_mile(self):
        scored = score_vehicle(make_listing(price=30000, shipping=2000, mileage=100000),
                               make_complete_history(), CRITERIA, CONFIG, 50000, 200000)
        # $32,000 landed over 100,000 remaining miles of a 200,000-mile expected life.
        assert round(scored.cost_per_remaining_mile, 3) == 0.320

    def test_landed_price_includes_shipping(self):
        assert make_listing(price=30000, shipping=1990).landed_price == 31990


class TestMergeReports(unittest.TestCase):
    def test_problem_from_either_vendor_wins(self):
        """The Tacoma case: Carfax saw a towed accident, AutoCheck reported none."""
        autocheck = HistoryReport(vin="V", status=STATUS_PARSED, vendor="autocheck",
                                  accident_reported=False, sources=["autocheck"])
        carfax = HistoryReport(vin="V", status=STATUS_PARSED, vendor="carfax",
                               accident_reported=True, sources=["carfax"])
        merged = merge_reports([autocheck, carfax])
        assert merged.accident_reported is True
        assert any("accident_reported" in conflict for conflict in merged.conflicts)

    def test_agreement_produces_no_conflict(self):
        a = HistoryReport(vin="V", status=STATUS_PARSED, vendor="autocheck",
                          accident_reported=False)
        b = HistoryReport(vin="V", status=STATUS_PARSED, vendor="carfax",
                          accident_reported=False)
        assert merge_reports([a, b]).conflicts == []

    def test_owner_count_takes_the_higher_estimate(self):
        a = HistoryReport(vin="V", status=STATUS_PARSED, vendor="autocheck", owner_count=1)
        b = HistoryReport(vin="V", status=STATUS_PARSED, vendor="carfax", owner_count=2)
        merged = merge_reports([a, b])
        assert merged.owner_count == 2
        assert any("owner_count" in conflict for conflict in merged.conflicts)

    def test_vendor_specific_signals_survive_the_merge(self):
        autocheck = HistoryReport(vin="V", status=STATUS_PARSED, vendor="autocheck",
                                  autocheck_score=95, autocheck_score_low=88,
                                  autocheck_score_high=93)
        carfax = HistoryReport(vin="V", status=STATUS_PARSED, vendor="carfax",
                               reliability_forecast="great", avg_annual_repair_cost=320)
        merged = merge_reports([autocheck, carfax])
        assert merged.autocheck_score == 95
        assert merged.reliability_forecast == "great"
        assert merged.avg_annual_repair_cost == 320
        assert merged.sources == ["autocheck", "carfax"]

    def test_blocked_preferred_over_unavailable_when_nothing_parsed(self):
        """Blocked is retryable and never cached; unavailable would be remembered as fact."""
        unavailable = HistoryReport(vin="V", status="history_unavailable", vendor="autocheck")
        blocked = HistoryReport(vin="V", status=STATUS_BLOCKED, vendor="carfax")
        assert merge_reports([unavailable, blocked]).status == STATUS_BLOCKED


if __name__ == "__main__":
    unittest.main()
