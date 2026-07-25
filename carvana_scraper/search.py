"""Stage 1 — discover filtered inventory from Carvana's search results pages.

Reads what a normal page load delivers: the vehicle records embedded in Carvana's RSC flight
payload (see rsc.py). No private JSON API is called, so there is no undocumented request contract
to drift out from under us.

**Filtering is deliberately split.** Make and model go through the URL path
(`/cars/toyota-4runner`), which is confirmed stable. Year, price and mileage are applied in Python
over the extracted records, because Carvana's query-param vocabulary for those is undocumented —
guessing it is the most drift-prone thing this tool could do, and arithmetic on records we already
hold cannot silently break.

Fails loudly on payload-shape drift: a silently empty or truncated result set would produce a
confident ranking over cars that were never actually considered.
"""

from __future__ import annotations

import re

from .browser import human_pause
from .models import Listing, SearchCriteria
from .rsc import PayloadShapeError, extract_vehicle_records

SEARCH_BASE = "https://www.carvana.com/cars"

# The zip Carvana prices against is NOT in the search page's HTML — it travels in the body of the
# pricing/filter XHRs the page fires. Shipping cost (and therefore landed price, and therefore the
# ranking) depends on it, so it is captured from those requests and any mismatch is surfaced
# rather than quietly accepted.
_ZIP_IN_PAYLOAD_RE = re.compile(r'"zip5"\s*:\s*"(\d{5})"')
_PRICING_HOST = "apik.carvana.io"


def slugify(value: str) -> str:
    """Convert a make or model name into a Carvana URL path segment.

    "Grand Highlander" -> "grand-highlander"
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return slug.strip("-")


def build_search_url(criteria: SearchCriteria, page_number: int = 1) -> str:
    """Build the search results URL for a page of results.

    Make/model become path segments; the page number is a query parameter. Year, price and
    mileage are intentionally absent — see the module docstring.
    """
    path = SEARCH_BASE
    segments = [slugify(part) for part in (criteria.make, criteria.model) if part]
    if segments:
        path = f"{SEARCH_BASE}/{'-'.join(segments)}"
    return path if page_number <= 1 else f"{path}?page={page_number}"


def _extract_priced_zip(text: str) -> str | None:
    """Pull a `"zip5":"NNNNN"` value out of a request body or page payload."""
    match = _ZIP_IN_PAYLOAD_RE.search(text or "")
    return match.group(1) if match else None


def _attach_zip_sniffer(browser_page, sink: dict) -> None:
    """Record the zip Carvana prices against, read from its own outgoing XHR bodies.

    Registered once per run. Playwright holds a strong reference to the handler, so the sink dict
    accumulates across every page load in the session.
    """
    def on_request(request) -> None:
        if _PRICING_HOST not in request.url:
            return
        try:
            body = request.post_data
        except Exception:
            return
        zip_code = _extract_priced_zip(body or "")
        if zip_code:
            sink.setdefault("priced_zip", zip_code)

    browser_page.on("request", on_request)


def collect_listings(
    browser_page,
    criteria: SearchCriteria,
    max_pages: int = 8,
    hard_limit: int | None = None,
    verbose: bool = True,
) -> tuple[list[Listing], dict]:
    """Page through search results and return the listings matching the criteria.

    Args:
        browser_page: A Playwright Page from an active browser session.
        criteria: The run's search criteria.
        max_pages: Safety bound on how many result pages to load.
        hard_limit: Stop once this many matching listings are found.
        verbose: Print per-page progress.

    Returns:
        (listings, stats) where stats records what happened at every stage — page count, raw
        record count, how many were filtered out, the zip actually priced against, and whether
        pagination appeared to work.

    Raises:
        PayloadShapeError: If the first page yields no extractable records. Later pages returning
            nothing is treated as the end of results, not an error.
    """
    seen_vins: set[str] = set()
    raw_records: list[dict] = []
    stats: dict = {
        "pages_loaded": 0,
        "raw_records": 0,
        "duplicate_records": 0,
        "pagination_effective": None,
        "priced_zip": None,
        "requested_zip": criteria.zip_code,
        "stopped_because": None,
    }

    zip_sink: dict = {}
    _attach_zip_sniffer(browser_page, zip_sink)

    for page_number in range(1, max_pages + 1):
        url = build_search_url(criteria, page_number)
        if verbose:
            print(f"  [search] page {page_number}: {url}")
        browser_page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        human_pause(3.0, 6.0)
        # Nudge the page so any lazily hydrated result batches render.
        for _ in range(2):
            browser_page.mouse.wheel(0, 2400)
            human_pause(1.0, 2.0)

        html = browser_page.content()
        stats["pages_loaded"] = page_number

        if stats["priced_zip"] is None:
            stats["priced_zip"] = zip_sink.get("priced_zip") or _extract_priced_zip(html)

        try:
            records = extract_vehicle_records(html, require_records=(page_number == 1))
        except PayloadShapeError:
            if page_number == 1:
                raise
            stats["stopped_because"] = f"page {page_number} yielded no records"
            break

        fresh = [r for r in records if str(r.get("vin", "")).upper() not in seen_vins]
        duplicates = len(records) - len(fresh)
        stats["duplicate_records"] += duplicates

        if page_number > 1 and not fresh:
            # Every VIN repeated: either we are past the last page, or ?page= is being ignored.
            stats["pagination_effective"] = False
            stats["stopped_because"] = (
                f"page {page_number} returned only VINs already seen — pagination ineffective "
                "or results exhausted"
            )
            break
        if page_number > 1 and fresh:
            stats["pagination_effective"] = True

        for record in fresh:
            seen_vins.add(str(record["vin"]).upper())
        raw_records.extend(fresh)
        if verbose:
            print(f"           +{len(fresh)} new records (total {len(raw_records)})")

        if not records:
            stats["stopped_because"] = f"page {page_number} was empty"
            break
        if hard_limit is not None and len(raw_records) >= hard_limit * 3:
            # Enough raw records that filtering will very likely satisfy the limit.
            stats["stopped_because"] = "raw record buffer large enough for the requested limit"
            break

    stats["raw_records"] = len(raw_records)

    listings: list[Listing] = []
    skipped_unparseable = 0
    for record in raw_records:
        try:
            listings.append(Listing.from_record(record))
        except (KeyError, TypeError, ValueError):
            skipped_unparseable += 1
    stats["unparseable_records"] = skipped_unparseable

    matching = [listing for listing in listings if criteria.matches(listing)]
    stats["parsed_listings"] = len(listings)
    stats["filtered_out"] = len(listings) - len(matching)
    stats["matched_before_limit"] = len(matching)

    # Record the truncation explicitly. A --limit that quietly drops matches is the same silent
    # narrowing the run manifest exists to expose, so it must be visible in the counts.
    stats["dropped_by_limit"] = 0
    if hard_limit is not None and len(matching) > hard_limit:
        stats["dropped_by_limit"] = len(matching) - hard_limit
        matching = matching[:hard_limit]
    stats["matched"] = len(matching)

    if stats["priced_zip"] and stats["priced_zip"] != criteria.zip_code:
        print(
            f"  [search] WARNING: Carvana priced these results against zip "
            f"{stats['priced_zip']}, not the requested {criteria.zip_code}. "
            "Shipping costs — and therefore landed prices — reflect that zip."
        )

    return matching, stats
