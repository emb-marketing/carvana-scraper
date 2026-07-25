"""Accept a manually obtained report, parse it, merge it, and rescore.

This is the app's highest-value capability and it needs no scraping at all: `parse_carfax_text` and
`parse_autocheck_text` are pure `str -> HistoryReport` functions that import only `re` and
`models`. So a report the browser could not fetch can be pasted in and take exactly the same path
as a scraped one — same parser, same pessimistic merge, same cache, same scorer.

**The input must be `document.body.innerText` shape**, i.e. visible text with its line breaks
intact. Both parsers locate sections by whole-line equality (`line == "Structural Damage"`) and
discard candidate verdict lines longer than 5 words (Carfax) or 7 (AutoCheck). A browser
select-all/copy produces exactly this.

That is also why HTML and PDF are refused rather than best-effort parsed. Neither would raise: they
would collapse the line structure, match no section labels, and yield a report whose every field is
None. Under invariant 1 that car is correctly held out of the ranking — but the operator would have
been told the paste "worked". Refusing up front is the honest failure.
"""

from __future__ import annotations

from typing import Any

from .. import scoring
from ..autocheck import parse_autocheck_text
from ..browser import CHALLENGE_MARKERS
from ..cache import archive_raw, put_history
from ..carfax import parse_carfax_text
from ..history import merge_reports
from ..models import STATUS_PARSED, HistoryReport, Listing
from .serialize import has_carfax

# Mirrors history._fetch_report_page's own floor. Real reports run 6-15 KB; anything under this is
# a challenge shell, a truncated copy, or the wrong thing entirely.
MIN_REPORT_CHARS = 1500

# "AutoCheck Score" appears in every AutoCheck capture and in none of the Carfax ones (verified
# across the 30 archived reports in cache/raw/), which makes it a reliable discriminator.
_AUTOCHECK_MARKER = "AutoCheck Score"

# Carfax section labels — the same ones carfax.CHECK_SECTIONS keys on. Requiring two of them
# guards against a stray page that merely mentions Carfax.
_CARFAX_MARKERS = ("Total Loss", "Structural Damage", "Airbag Deployment", "Odometer Check",
                   "Accident / Damage", "Damage Brands", "CARFAX")

# Markup that means the operator pasted source rather than rendered text.
_MARKUP_MARKERS = ("<!doctype html", "<html", "<script", "<div ", "%pdf-")

VENDOR_CARFAX = "carfax"
VENDOR_AUTOCHECK = "autocheck"


class IngestError(ValueError):
    """A paste that must be refused, carrying an operator-facing explanation."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def detect_vendor(text: str) -> str | None:
    """Identify which vendor's report this text is, or None if it is neither."""
    if _AUTOCHECK_MARKER in text:
        return VENDOR_AUTOCHECK
    hits = sum(1 for marker in _CARFAX_MARKERS if marker in text)
    return VENDOR_CARFAX if hits >= 2 else None


def validate(text: str, vin: str) -> None:
    """Reject a paste that cannot produce a trustworthy report.

    Raises:
        IngestError: With a message explaining what to do differently.
    """
    if not vin:
        raise IngestError("No VIN given for this paste.")

    stripped = text.strip()
    if not stripped:
        raise IngestError("Nothing was pasted.")

    lowered = stripped[:4000].lower()
    for marker in _MARKUP_MARKERS:
        if marker in lowered:
            raise IngestError(
                "That looks like HTML or PDF source, not the report's visible text.",
                hint="Open the report in the browser, select all (Cmd-A), copy (Cmd-C) and paste "
                     "that. The parsers read the rendered text layout, and markup would parse to "
                     "an all-unknown report rather than failing outright.",
            )

    if len(stripped) < MIN_REPORT_CHARS:
        raise IngestError(
            f"Only {len(stripped)} characters — a real report is 6,000-15,000.",
            hint="This is usually a challenge page or a partial selection. Make sure the full "
                 "report rendered before copying.",
        )

    haystack = stripped.lower()
    found = [marker for marker in CHALLENGE_MARKERS if marker in haystack]
    if found:
        raise IngestError(
            f"That text is an anti-bot challenge page, not a report ({found[0]}).",
            hint="Solve the puzzle in the Chrome window first, then copy the report once it "
                 "renders. Recording this would cache a block as though it were history.",
        )

    if detect_vendor(stripped) is None:
        raise IngestError(
            "Could not tell whether this is a Carfax or an AutoCheck report.",
            hint="Expected section headings like 'Structural Damage' or 'AutoCheck Score'. "
                 "Check the whole report was copied, including its headings.",
        )


def parse_paste(text: str, vin: str, vendor: str | None = None) -> HistoryReport:
    """Parse pasted report text into a HistoryReport.

    Args:
        text: The report's visible text.
        vin: The VIN this paste belongs to. Always passed explicitly — both parsers would
            otherwise self-extract a VIN from the text, so a mis-paste would be filed under
            whichever car the operator actually copied.
        vendor: Override for the detected vendor.

    Returns:
        The parsed report. May legitimately be incomplete; callers must surface that.
    """
    stripped = text.strip()
    resolved = vendor or detect_vendor(stripped)
    vin = vin.upper()

    if resolved == VENDOR_AUTOCHECK:
        return parse_autocheck_text(stripped, vin=vin, source_url=f"pasted:autocheck:{vin}")
    return parse_carfax_text(stripped, vin=vin, source_url=f"pasted:carfax:{vin}")


def ingest(
    text: str,
    listing: Listing,
    existing: HistoryReport | None,
    connection,
    vendor: str | None = None,
) -> dict[str, Any]:
    """Validate, parse, merge and persist a pasted report for one vehicle.

    Args:
        text: Pasted report text.
        listing: The vehicle it belongs to.
        existing: The report already held for this VIN, if any (typically AutoCheck-only).
        connection: Open cache connection, owned by the calling thread.
        vendor: Override for the detected vendor.

    Returns:
        A summary for the UI: which vendor was read, whether the merged report is now complete,
        which decision fields are still missing, and any vendor conflicts the merge surfaced.

    Raises:
        IngestError: If the paste is not a usable report.
    """
    validate(text, listing.vin)
    parsed = parse_paste(text, listing.vin, vendor)
    resolved_vendor = VENDOR_AUTOCHECK if parsed.vendor == VENDOR_AUTOCHECK else VENDOR_CARFAX

    if not parsed.is_parsed:
        raise IngestError(
            f"The text was read but produced no usable {resolved_vendor} report.",
            hint="The section headings may not have survived the copy. Paste the report's plain "
                 "visible text, keeping its line breaks.",
        )

    # AutoCheck first, deliberately: merge_reports resolves _FIRST_WINS_FIELDS and source_url by
    # order, and AutoCheck-first is what the scraped pipeline does. Reversing it here would make a
    # pasted car's merged report differ from an identical scraped one.
    if existing is not None and existing.is_parsed:
        if resolved_vendor == VENDOR_CARFAX:
            merged = merge_reports([existing, parsed])
        else:
            merged = merge_reports([parsed, existing])
    else:
        merged = parsed

    # Archived under the same name the scraper uses, so the pasted report is available to the
    # Claude reviewer and to `--no-carfax` reruns exactly like a fetched one.
    archive_raw(listing.vin, text.strip(), extension=f"{resolved_vendor}.txt")
    put_history(connection, listing.vin, merged.status, vendor=merged.vendor,
                payload=merged.to_dict(), source_url=merged.source_url)

    return {
        "vin": listing.vin,
        "label": listing.label,
        "vendor": resolved_vendor,
        "status": merged.status,
        "merged_vendor": merged.vendor,
        "is_complete": merged.is_complete,
        "has_carfax": has_carfax(merged),
        "missing_decision_fields": list(merged.missing_decision_fields),
        "unrecognized_sections": list(merged.unrecognized_sections),
        "conflicts": list(merged.conflicts),
        "owner_count": merged.owner_count,
        "accident_reported": merged.accident_reported,
        "cached": merged.status == STATUS_PARSED,
        "report": merged,
    }


def rescore(result, config: dict) -> tuple[list, dict]:
    """Rescore a whole run after its histories changed.

    Returns (scored, anchor_info). Recomputes from scratch rather than patching one vehicle: the
    score anchors come from --max-price/--max-miles, so a newly complete car changes nothing about
    the others' scores, and rebuilding proves that rather than assuming it.
    """
    pairs = [(listing, result.histories[listing.vin],
              result.imperfection_counts.get(listing.vin))
             for listing in result.listings if listing.vin in result.histories]
    scored, anchor_info = scoring.score_all(pairs, result.criteria, config)
    for vehicle in scored:
        vehicle.carfax_attempted = vehicle.listing.vin in result.shortlist_vins
    return scored, anchor_info
