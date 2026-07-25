"""Turn the pipeline's dataclasses into JSON-safe dicts for the browser.

`dataclasses.asdict` is not enough. `Listing` keeps most of what a UI wants to display in computed
properties — landed price, price vs KBB, miles per year, the label, and the three URLs — and asdict
omits every one of them. So the fields are injected explicitly here.

Pure functions over already-fetched objects: no browser, no network, no I/O.
"""

from __future__ import annotations

from typing import Any

from ..history import has_carfax
from ..models import HistoryReport, Listing, ScoredVehicle
from ..report import RunManifest

__all__ = ["has_carfax", "listing_to_dict", "history_to_dict", "vehicle_to_dict",
           "manifest_to_dict", "needs_carfax_entry", "buckets"]

# HistoryReport.to_dict() iterates __dict__, which includes DECISION_FIELDS — it is annotated
# inside the dataclass body and so became a real field. Harmless in the cache payload, but noise
# in an API response, so it is dropped here rather than shipped to the browser.
_HISTORY_NOISE_KEYS = frozenset({"DECISION_FIELDS"})


def listing_to_dict(listing: Listing) -> dict[str, Any]:
    """Serialize a Listing, including its computed properties."""
    return {
        "vin": listing.vin,
        "vehicle_id": listing.vehicle_id,
        "year": listing.year,
        "make": listing.make,
        "model": listing.model,
        "trim": listing.trim,
        "mileage": listing.mileage,
        "price": listing.price,
        "shipping_fee": listing.shipping_fee,
        "kbb_value": listing.kbb_value,
        "msrp": listing.msrp,
        "market_adjustment": listing.market_adjustment,
        "stock_number": listing.stock_number,
        "tags": list(listing.tags),
        # Computed — absent from dataclasses.asdict, and most of what the table shows.
        "landed_price": listing.landed_price,
        "price_vs_kbb": listing.price_vs_kbb,
        "miles_per_year": listing.miles_per_year,
        "label": listing.label,
        "listing_url": listing.listing_url,
        "carfax_url": listing.carfax_url,
        "autocheck_url": listing.autocheck_url,
    }


def history_to_dict(report: HistoryReport | None) -> dict[str, Any] | None:
    """Serialize a HistoryReport, plus the derived completeness flags the UI needs."""
    if report is None:
        return None
    payload = {key: value for key, value in report.to_dict().items()
               if key not in _HISTORY_NOISE_KEYS}
    payload.update({
        "is_parsed": report.is_parsed,
        "is_complete": report.is_complete,
        "needs_review": report.needs_review,
        "missing_decision_fields": list(report.missing_decision_fields),
        "has_carfax": has_carfax(report),
    })
    return payload


def vehicle_to_dict(vehicle: ScoredVehicle) -> dict[str, Any]:
    """Serialize a ScoredVehicle for the ranking table."""
    return {
        "listing": listing_to_dict(vehicle.listing),
        "history": history_to_dict(vehicle.history),
        "score": vehicle.score,
        "cost_per_remaining_mile": vehicle.cost_per_remaining_mile,
        "components": dict(vehicle.components),
        "positives": list(vehicle.positives),
        "negatives": list(vehicle.negatives),
        "disqualifiers": list(vehicle.disqualifiers),
        "carfax_attempted": vehicle.carfax_attempted,
        "is_disqualified": vehicle.is_disqualified,
        "is_rankable": vehicle.is_rankable,
        "completeness_marker": vehicle.completeness_marker,
    }


def manifest_to_dict(manifest: RunManifest, reconcile: bool = True) -> dict[str, Any]:
    """Serialize the run manifest, including its reconciliation verdict.

    `reconciliation_problems` is included because it is the whole point of the manifest: silent
    narrowing is the failure mode that matters, so the UI must be able to show it, not just the
    raw counters.

    Args:
        manifest: The run's manifest.
        reconcile: False for a run that deliberately skipped the history stages (--search-only,
            --no-history). Those checks assume a full pipeline ran, so reporting "no AutoCheck
            report parsed" as a fault would be wrong — the operator asked for that. The CLI never
            showed a manifest in those modes at all.
    """
    return {
        "criteria": manifest.criteria,
        "counters": {
            "pages_loaded": manifest.pages_loaded,
            "raw_records": manifest.raw_records,
            "parsed_listings": manifest.parsed_listings,
            "filtered_out": manifest.filtered_out,
            "matched_before_limit": manifest.matched_before_limit,
            "dropped_by_limit": manifest.dropped_by_limit,
            "matched": manifest.matched,
            "shortlisted": manifest.shortlisted,
            "autocheck_parsed": manifest.autocheck_parsed,
            "carfax_attempted": manifest.carfax_attempted,
            "carfax_parsed": manifest.carfax_parsed,
            "carfax_blocked": manifest.carfax_blocked,
            "autocheck_blocked": getattr(manifest, "autocheck_blocked", 0),
            "history_unavailable": manifest.history_unavailable,
            "from_cache": manifest.from_cache,
            "disqualified": manifest.disqualified,
            "ranked": manifest.ranked,
            "needs_carfax": manifest.needs_carfax,
            "conflicts": manifest.conflicts,
        },
        "anchor_note": manifest.anchor_note,
        "warnings": list(manifest.warnings),
        "reconciliation_problems": list(manifest.reconciliation_problems()) if reconcile else [],
        "reconciled": reconcile,
        "lines": list(manifest.lines()),
    }


def needs_carfax_entry(vehicle: ScoredVehicle, carfax_skipped: bool = False) -> dict[str, Any]:
    """One row of the 'needs your help' list.

    `remedy` mirrors the report's split — a vehicle whose Carfax was attempted and blocked is
    retryable or pasteable, while one that never made the shortlist needs a higher --top-n — with
    one addition the text report does not make: when the whole Carfax stage was skipped, "raise
    --top-n" would send the operator to the wrong control.
    """
    if vehicle.carfax_attempted:
        remedy = "paste_or_retry"
    elif carfax_skipped:
        remedy = "carfax_skipped"
    else:
        remedy = "raise_top_n"
    return {
        "vin": vehicle.listing.vin,
        "label": vehicle.listing.label,
        "carfax_url": vehicle.listing.carfax_url,
        "autocheck_url": vehicle.listing.autocheck_url,
        "listing_url": vehicle.listing.listing_url,
        "landed_price": vehicle.listing.landed_price,
        "mileage": vehicle.listing.mileage,
        "carfax_attempted": vehicle.carfax_attempted,
        "remedy": remedy,
        "missing_decision_fields": list(vehicle.history.missing_decision_fields),
    }


def buckets(scored: list[ScoredVehicle], sort_key=None,
            carfax_skipped: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Split scored vehicles into the report's three buckets.

    Mirrors `report.render` exactly so the UI and the markdown can never disagree about which car
    is where.
    """
    ranked = [v for v in scored if v.is_rankable]
    if sort_key is not None:
        ranked = sorted(ranked, key=sort_key)
    return {
        "ranked": [vehicle_to_dict(v) for v in ranked],
        "needs_carfax": [needs_carfax_entry(v, carfax_skipped) for v in scored
                         if not v.is_disqualified and not v.is_rankable],
        "disqualified": [vehicle_to_dict(v) for v in scored if v.is_disqualified],
    }
