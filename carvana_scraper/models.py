"""Shared data model: a Carvana listing, a parsed history report, and a scored result.

Kept in one module because search, history, scoring and report all speak these types, and the
completeness contract below has to mean the same thing everywhere.

**The completeness contract.** Every history field is three-valued: True (problem present),
False (explicitly reported clean), or None (unknown — not read, not parsed, or challenged).
`None` must never be treated as `False`. A vehicle whose history could not be read is not a
vehicle with a clean history, and scoring enforces that distinction rather than trusting callers
to remember it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CARFAX_URL_TEMPLATE = "https://www.carfax.com/VehicleHistory/p/Report.cfx?partner=CVN_0&vin={vin}"
VDP_URL_TEMPLATE = "https://www.carvana.com/vehicle/{vehicle_id}"
AUTOCHECK_URL_TEMPLATE = "https://www.carvana.com/vehicle/autocheck/{vehicle_id}"

# History fetch outcomes. Only these three; there is no implicit fourth "probably fine" state.
STATUS_PARSED = "parsed"
STATUS_BLOCKED = "history_blocked"
STATUS_UNAVAILABLE = "history_unavailable"


@dataclass(frozen=True)
class SearchCriteria:
    """What to shop for on this run. Supplied per run as CLI flags, never a stored config.

    `max_price` and `max_miles` are load-bearing beyond filtering: scoring normalizes against
    them, which is what makes a score mean the same thing across runs.
    """

    make: str | None = None
    model: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    max_price: float | None = None
    max_miles: int | None = None
    zip_code: str = "89002"
    top_n: int = 12
    max_reports: int = 40

    def matches(self, listing: "Listing") -> bool:
        """Whether a listing satisfies the numeric criteria.

        Make and model are filtered server-side via the search URL path; year, price and mileage
        are applied here because Carvana's private query-param vocabulary for them is
        undocumented and guessing it is the most drift-prone thing this tool could do.
        """
        if self.year_min is not None and listing.year < self.year_min:
            return False
        if self.year_max is not None and listing.year > self.year_max:
            return False
        if self.max_price is not None and listing.landed_price > self.max_price:
            return False
        if self.max_miles is not None and listing.mileage > self.max_miles:
            return False
        return True

    def describe(self) -> str:
        """One-line human summary for the report header."""
        parts: list[str] = []
        if self.make or self.model:
            parts.append(" ".join(filter(None, (self.make, self.model))))
        else:
            parts.append("any make/model")
        if self.year_min or self.year_max:
            parts.append(f"{self.year_min or '…'}-{self.year_max or '…'}")
        if self.max_price is not None:
            parts.append(f"≤${self.max_price:,.0f} landed")
        if self.max_miles is not None:
            parts.append(f"≤{self.max_miles:,} mi")
        parts.append(f"zip {self.zip_code}")
        return " · ".join(parts)


@dataclass(frozen=True)
class Listing:
    """A vehicle as Carvana's search results page describes it.

    Every field here comes from the RSC flight payload, so it is available for free on a normal
    page load — no per-vehicle request required.
    """

    vin: str
    vehicle_id: int
    year: int
    make: str
    model: str
    trim: str | None
    mileage: int
    price: float
    shipping_fee: float
    kbb_value: float | None
    msrp: float | None
    market_adjustment: float | None
    stock_number: int | None = None
    vdp_slug: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def landed_price(self) -> float:
        """Purchase price plus Carvana's transport cost — what actually leaves the bank."""
        return self.price + self.shipping_fee

    @property
    def price_vs_kbb(self) -> float | None:
        """Landed price minus KBB value. Negative is under book, positive is over."""
        if self.kbb_value is None:
            return None
        return self.landed_price - self.kbb_value

    @property
    def miles_per_year(self) -> float | None:
        """Average annual mileage, using 2026 as the reference year."""
        age = max(2026 - self.year, 1)
        return self.mileage / age

    @property
    def label(self) -> str:
        return f"{self.year} {self.make} {self.model}" + (f" {self.trim}" if self.trim else "")

    @property
    def listing_url(self) -> str:
        return VDP_URL_TEMPLATE.format(vehicle_id=self.vehicle_id)

    @property
    def carfax_url(self) -> str:
        return CARFAX_URL_TEMPLATE.format(vin=self.vin)

    @property
    def autocheck_url(self) -> str:
        return AUTOCHECK_URL_TEMPLATE.format(vehicle_id=self.vehicle_id)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Listing":
        """Build a Listing from a raw RSC vehicle record.

        Args:
            record: One vehicle object recovered by rsc.extract_vehicle_records.

        Raises:
            KeyError: If a required field is absent. Callers should let this surface — a missing
                VIN or price means the payload contract changed.
        """
        price_block = record.get("price") or {}
        shipping = price_block.get("transportCost")
        if shipping is None:
            shipping = record.get("transportCost") or 0
        tags = tuple(
            tag.get("tagKey", "") for tag in (record.get("vehicleTags") or [])
            if isinstance(tag, dict)
        )
        return cls(
            vin=str(record["vin"]).upper(),
            vehicle_id=int(record["vehicleId"]),
            year=int(record["year"]),
            make=str(record["make"]),
            model=str(record["model"]),
            trim=record.get("trim"),
            mileage=int(record["mileage"]),
            price=float(price_block["total"]),
            shipping_fee=float(shipping),
            kbb_value=_opt_float(price_block.get("kbbValue")),
            msrp=_opt_float(price_block.get("msrp")),
            market_adjustment=_opt_float(price_block.get("marketAdjustment")),
            stock_number=record.get("stockNumber"),
            vdp_slug=record.get("vdpSlug"),
            tags=tags,
        )


@dataclass
class HistoryReport:
    """A parsed vehicle-history report, or an explicit record of why there isn't one.

    Tri-state fields use None for "unknown". See the completeness contract in the module
    docstring — this is the single most important invariant in the project.
    """

    vin: str
    status: str
    vendor: str | None = None
    source_url: str | None = None

    owner_count: int | None = None
    accident_count: int | None = None
    accident_reported: bool | None = None
    damage_reported: bool | None = None
    total_loss: bool | None = None
    structural_damage: bool | None = None
    airbag_deployment: bool | None = None
    odometer_rollback: bool | None = None
    odometer_reading: int | None = None
    title_brands: list[str] = field(default_factory=list)
    title_brand_problem: bool | None = None
    service_record_count: int | None = None
    detailed_record_count: int | None = None
    use_types: list[str] = field(default_factory=list)
    open_recalls: bool | None = None

    # Carfax-only signals.
    reliability_forecast: str | None = None
    avg_annual_repair_cost: int | None = None

    # AutoCheck-only signals. `auction_problem` and `insurance_loss` are disqualifier-grade:
    # a title transferred to an insurer usually means the car was totalled.
    auction_problem: bool | None = None
    insurance_loss: bool | None = None
    autocheck_score: int | None = None
    autocheck_score_low: int | None = None
    autocheck_score_high: int | None = None

    # Vendors that contributed to this report, in merge order.
    sources: list[str] = field(default_factory=list)
    # Fields where the two vendors disagreed, e.g. "accident_reported: carfax=True autocheck=False".
    conflicts: list[str] = field(default_factory=list)

    # Sections whose verdict text the parser did not recognize. Non-empty means a human should
    # look at the report: the parser is calibrated on clean reports, so an unfamiliar verdict is
    # treated as a possible problem rather than silently ignored.
    unrecognized_sections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # Fields that must be known for a vehicle to be safely rankable.
    DECISION_FIELDS: tuple[str, ...] = (
        "owner_count", "accident_reported", "title_brand_problem",
        "total_loss", "structural_damage", "airbag_deployment", "odometer_rollback",
    )

    @property
    def is_parsed(self) -> bool:
        return self.status == STATUS_PARSED

    @property
    def missing_decision_fields(self) -> list[str]:
        """Decision-critical fields still unknown."""
        return [name for name in self.DECISION_FIELDS if getattr(self, name) is None]

    @property
    def is_complete(self) -> bool:
        """True only when the report was parsed AND every decision field is known."""
        return self.is_parsed and not self.missing_decision_fields

    @property
    def needs_review(self) -> bool:
        """True when a human should read the report before trusting the score."""
        return bool(self.unrecognized_sections) or not self.is_complete

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the cache. Properties are recomputed on load, so only fields go."""
        return {
            key: value for key, value in self.__dict__.items()
            if not key.startswith("_")
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryReport":
        """Rebuild from a cached dict, tolerating fields added since it was written."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known.setdefault("vin", data.get("vin", ""))
        known.setdefault("status", data.get("status", STATUS_UNAVAILABLE))
        return cls(**known)


@dataclass
class ScoredVehicle:
    """A listing plus its history, score and the reasons behind the score."""

    listing: Listing
    history: HistoryReport
    score: float | None = None
    cost_per_remaining_mile: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    # Whether this run tried to fetch a Carfax report for this vehicle. Distinguishes "blocked,
    # re-run to fill the gap" from "never shortlisted, raise --top-n" — advice that differs.
    carfax_attempted: bool = False

    @property
    def is_disqualified(self) -> bool:
        return bool(self.disqualifiers)

    @property
    def is_rankable(self) -> bool:
        """Only a disqualifier-free vehicle with a complete history enters the main ranking."""
        return not self.is_disqualified and self.history.is_complete

    @property
    def completeness_marker(self) -> str:
        """Compact flag for the report table."""
        if self.is_disqualified:
            return "DQ"
        if not self.history.is_parsed:
            return "?"
        if self.history.needs_review:
            return "!"
        return ""


def _opt_float(value: Any) -> float | None:
    """Coerce to float, returning None for absent or unparseable values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
