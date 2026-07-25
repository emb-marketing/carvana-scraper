"""Stage 4a — hard disqualifiers and a run-stable weighted score.

Pure functions. No network, no browser; the only I/O is reading config/scoring.json.

**Scores anchor to the run's own criteria, not to the day's results.** Price is normalized against
`--max-price` and mileage against `--max-miles`. Min-max normalizing within the returned candidate
set — the obvious approach — would make an identical vehicle score 82 one day and 61 the next
purely because the surrounding inventory changed, quietly destroying comparability across runs.
When an anchor is missing, the observed maximum is used as a fallback and the result is explicitly
flagged as unanchored rather than passed off as stable.

**Missing signals are renormalized away, not scored as zero.** A vehicle without a KBB value is
scored on the signals it does have, with the remaining weights scaled up. Otherwise a data gap
would masquerade as a bad car.

**A vehicle whose history could not be read is never scored as clean.** Unknown is not the same as
good; `ScoredVehicle.is_rankable` requires a complete history, and the caller keeps those vehicles
out of the main ranking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import HistoryReport, Listing, ScoredVehicle, SearchCriteria

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "scoring.json"


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load the scoring configuration.

    Raises:
        FileNotFoundError: If the config is missing — it is required, not optional, so that a
            silently-defaulted weighting can never explain a surprising ranking.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Scoring config not found at {path}. It defines the weights and disqualifiers; "
            "running without it would produce a ranking nobody could audit.")
    return json.loads(path.read_text(encoding="utf-8"))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def find_disqualifiers(history: HistoryReport, config: dict[str, Any]) -> list[str]:
    """Return human-readable reasons this vehicle should be excluded outright.

    Only fields explicitly reported as problems count. A None (unknown) never disqualifies —
    unknown history is handled by the completeness rule instead, which keeps the vehicle out of
    the ranking for a different and clearly-stated reason.
    """
    enabled = config.get("disqualifiers", {})
    labels = {
        "title_brand_problem": "branded title (salvage/junk/rebuilt/flood/lemon)",
        "odometer_rollback": "odometer rollback or discrepancy",
        "structural_damage": "structural/frame damage",
        "airbag_deployment": "airbag deployment",
        "total_loss": "total loss record",
        "insurance_loss": "insurance total loss / title transferred to insurer",
        "auction_problem": "auction-reported issue",
    }
    reasons: list[str] = []
    for field_name, label in labels.items():
        if not enabled.get(field_name):
            continue
        if getattr(history, field_name, None) is True:
            reasons.append(label)
    return reasons


def _accident_component(history: HistoryReport, config: dict) -> float | None:
    """Goodness for accident history: 1.0 means none reported."""
    table = config["accident_severity_scores"]
    if history.accident_reported is None and history.accident_count is None:
        return None
    if history.accident_reported is False or history.accident_count == 0:
        return table["none"]
    severity = next(
        (note.split(":", 1)[1].strip() for note in history.notes
         if note.startswith("accident severity:")), None)
    if severity and severity in table:
        return table[severity]
    return table["unknown"]


def _owners_component(history: HistoryReport, config: dict) -> float | None:
    if history.owner_count is None:
        return None
    table = config["owner_count_scores"]
    return table.get(str(history.owner_count), table["default"])


def _use_type_component(history: HistoryReport, config: dict) -> float | None:
    """Worst use type wins — a car that was both leased and rented is a rental."""
    if not history.use_types:
        return None
    table = config["use_type_scores"]
    scores = [table[use] for use in history.use_types if use in table]
    return min(scores) if scores else None


def _autocheck_component(history: HistoryReport) -> float | None:
    """AutoCheck Score relative to the band comparable vehicles fall in.

    The peer range is what makes this meaningful: a 93 is excellent against a 76-86 band and
    unremarkable against 91-96.
    """
    if history.autocheck_score is None:
        return None
    low, high = history.autocheck_score_low, history.autocheck_score_high
    if low is not None and high is not None and high > low:
        return _clamp((history.autocheck_score - low) / (high - low))
    return _clamp(history.autocheck_score / 100.0)


def _kbb_component(listing: Listing) -> float | None:
    """Landed price against KBB value: 10% under book scores 1.0, 10% over scores 0.0."""
    if not listing.kbb_value:
        return None
    pct_over = (listing.landed_price - listing.kbb_value) / listing.kbb_value
    return _clamp(0.5 - pct_over * 5.0)


def _service_component(listing: Listing, history: HistoryReport, config: dict) -> float | None:
    """Service records per year against a target cadence."""
    if history.service_record_count is None:
        return None
    age_years = max(2026 - listing.year, 1)
    per_year = history.service_record_count / age_years
    return _clamp(per_year / config["target_service_records_per_year"])


def _repair_cost_component(history: HistoryReport, config: dict) -> float | None:
    if history.avg_annual_repair_cost is None:
        return None
    best, worst = config["repair_cost_best"], config["repair_cost_worst"]
    if worst <= best:
        return None
    return _clamp((worst - history.avg_annual_repair_cost) / (worst - best))


def _imperfection_component(imperfection_count: int | None, config: dict) -> float | None:
    if imperfection_count is None:
        return None
    zero_at = config["imperfection_count_for_zero_score"]
    return _clamp(1.0 - imperfection_count / zero_at)


def score_vehicle(
    listing: Listing,
    history: HistoryReport,
    criteria: SearchCriteria,
    config: dict[str, Any],
    price_anchor: float,
    miles_anchor: float,
    imperfection_count: int | None = None,
) -> ScoredVehicle:
    """Score one vehicle, producing components and human-readable reasons.

    Args:
        listing: The vehicle.
        history: Its merged history report.
        criteria: The run's criteria (used for anchors and reporting).
        config: Loaded scoring config.
        price_anchor: Landed price scoring 0.0. Normally `criteria.max_price`.
        miles_anchor: Mileage scoring 0.0. Normally `criteria.max_miles`.
        imperfection_count: Number of Carvana-photographed cosmetic imperfections, if fetched.

    Returns:
        A ScoredVehicle. `score` is None when the vehicle is disqualified or its history is
        incomplete — such vehicles are reported separately, never ranked as if scored.
    """
    scored = ScoredVehicle(listing=listing, history=history)
    scored.disqualifiers = find_disqualifiers(history, config)

    if listing.mileage < config["expected_life_miles"]:
        scored.cost_per_remaining_mile = (
            listing.landed_price / (config["expected_life_miles"] - listing.mileage))

    components: dict[str, float | None] = {
        "price": _clamp(1.0 - listing.landed_price / price_anchor) if price_anchor else None,
        "mileage": _clamp(1.0 - listing.mileage / miles_anchor) if miles_anchor else None,
        "kbb_delta": _kbb_component(listing),
        "accidents": _accident_component(history, config),
        "autocheck_score": _autocheck_component(history),
        "owners": _owners_component(history, config),
        "use_type": _use_type_component(history, config),
        "service_density": _service_component(listing, history, config),
        "repair_cost": _repair_cost_component(history, config),
        "reliability": (config["reliability_scores"].get(history.reliability_forecast)
                        if history.reliability_forecast else None),
        "imperfections": _imperfection_component(imperfection_count, config),
        "recalls": (None if history.open_recalls is None
                    else (0.0 if history.open_recalls else 1.0)),
    }
    scored.components = {name: value for name, value in components.items() if value is not None}

    # Renormalize over the signals actually present so a data gap is not a penalty.
    weights = config["weights"]
    available_weight = sum(weights.get(name, 0.0) for name in scored.components)
    if available_weight > 0:
        weighted = sum(weights.get(name, 0.0) * value
                       for name, value in scored.components.items())
        raw_score = 100.0 * weighted / available_weight
    else:
        raw_score = 0.0

    if not scored.is_disqualified and history.is_complete:
        scored.score = round(raw_score, 1)

    _attach_reasons(scored, listing, history, imperfection_count)
    return scored


def _attach_reasons(
    scored: ScoredVehicle,
    listing: Listing,
    history: HistoryReport,
    imperfection_count: int | None,
) -> None:
    """Populate the plain-language positives and negatives shown under each top result."""
    if listing.price_vs_kbb is not None:
        delta = listing.price_vs_kbb
        target = scored.positives if delta < 0 else scored.negatives
        target.append(f"${abs(delta):,.0f} {'under' if delta < 0 else 'over'} KBB "
                      f"(${listing.kbb_value:,.0f})")

    if history.accident_reported is False:
        scored.positives.append("no accidents reported")
    elif history.accident_reported is True:
        severity = next((note.split(":", 1)[1].strip() for note in history.notes
                         if note.startswith("accident severity:")), "unspecified")
        scored.negatives.append(f"accident reported ({severity})")

    if history.owner_count == 1:
        scored.positives.append("single owner")
    elif history.owner_count and history.owner_count >= 3:
        scored.negatives.append(f"{history.owner_count} owners")

    worst_use = None
    if history.use_types:
        for candidate in ("taxi", "police", "commercial", "rental", "fleet"):
            if candidate in history.use_types:
                worst_use = candidate
                break
    if worst_use:
        scored.negatives.append(f"{worst_use} use history")
    elif "personal" in history.use_types:
        scored.positives.append("personal use only")

    if history.autocheck_score is not None and history.autocheck_score_high is not None:
        if history.autocheck_score >= history.autocheck_score_high:
            scored.positives.append(
                f"AutoCheck {history.autocheck_score} (above peer range "
                f"{history.autocheck_score_low}-{history.autocheck_score_high})")
        elif (history.autocheck_score_low is not None
                and history.autocheck_score < history.autocheck_score_low):
            scored.negatives.append(
                f"AutoCheck {history.autocheck_score} (below peer range "
                f"{history.autocheck_score_low}-{history.autocheck_score_high})")

    if history.reliability_forecast == "great":
        scored.positives.append("great reliability forecast")
    elif history.reliability_forecast == "fair":
        scored.negatives.append("fair reliability forecast")

    if history.avg_annual_repair_cost:
        repair_note = f"${history.avg_annual_repair_cost}/yr est. repairs"
        if history.avg_annual_repair_cost > 500:
            scored.negatives.append(repair_note)
        else:
            scored.positives.append(repair_note)

    if history.open_recalls:
        scored.negatives.append("open recall")
    if history.service_record_count is not None and history.service_record_count >= 10:
        scored.positives.append(f"{history.service_record_count} service records")
    if imperfection_count is not None and imperfection_count >= 8:
        scored.negatives.append(f"{imperfection_count} cosmetic imperfections")

    if listing.miles_per_year and listing.miles_per_year > 18000:
        scored.negatives.append(f"{listing.miles_per_year:,.0f} mi/yr (high)")


def score_all(
    pairs: list[tuple[Listing, HistoryReport, int | None]],
    criteria: SearchCriteria,
    config: dict[str, Any],
) -> tuple[list[ScoredVehicle], dict[str, Any]]:
    """Score every vehicle and report how the anchors were chosen.

    Returns:
        (scored_vehicles, anchor_info). `anchor_info["anchored"]` is False when a criteria anchor
        was missing and the observed maximum was substituted — in which case scores are comparable
        within this run but NOT across runs, and the report says so.
    """
    price_anchor = criteria.max_price
    miles_anchor = float(criteria.max_miles) if criteria.max_miles else None
    anchored = price_anchor is not None and miles_anchor is not None

    if price_anchor is None:
        price_anchor = max((listing.landed_price for listing, _, _ in pairs), default=1.0) or 1.0
    if miles_anchor is None:
        miles_anchor = float(max((listing.mileage for listing, _, _ in pairs), default=1)) or 1.0

    scored = [
        score_vehicle(listing, history, criteria, config,
                      price_anchor=price_anchor, miles_anchor=miles_anchor,
                      imperfection_count=imperfections)
        for listing, history, imperfections in pairs
    ]
    return scored, {
        "anchored": anchored,
        "price_anchor": price_anchor,
        "miles_anchor": miles_anchor,
        "note": ("scores anchored to --max-price/--max-miles and comparable across runs"
                 if anchored else
                 "NO --max-price/--max-miles given: anchored to this run's observed maximums, "
                 "so scores are NOT comparable across runs"),
    }
