"""Stage 3 — fetch and merge vehicle-history reports from two vendors.

**Why two vendors.** Neither is a superset of the other. On a 2025 Tacoma, Carfax reported a towed
accident (10/17/2025, minor-to-moderate damage) that AutoCheck rendered as "No Accidents or Damage
Reported". AutoCheck alone would have scored that truck clean and could have ranked it first.

**Division of labour, set by what the sites actually allow:**

| Vendor | Coverage | Why |
|---|---|---|
| AutoCheck | every vehicle | Carvana-hosted, 10/10 usable in recon, no rate limit, no CAPTCHA |
| Carfax | the top-N shortlist | DataDome puzzles after ~6 fetches per session; one solve restored access for 7+ consecutive fetches |

**Merge rule: pessimistic.** If either vendor reports a problem, the vehicle carries that problem.
Disagreements are recorded in `conflicts` and surfaced in the report rather than silently resolved.
Owner count takes the higher of the two — both vendors call it an estimate, and more owners is the
less flattering reading.

Every vehicle ends in exactly one of three explicit states — `parsed`, `history_blocked`,
`history_unavailable`. A blocked vehicle is never cached, so it retries on the next run, and it is
never scored as though its history were clean.
"""

from __future__ import annotations

import threading
from typing import Callable

from .autocheck import parse_autocheck_text
from .browser import detect_challenge, human_pause, looks_like_report, page_text, wait_for_manual_assist
from .cache import archive_raw, get_history, put_history
from .carfax import parse_carfax_text
from .models import (
    STATUS_BLOCKED, STATUS_PARSED, STATUS_UNAVAILABLE, HistoryReport, Listing,
)
from .vdp import fetch_autocheck_report_url

# Tri-state problem fields where True beats False in a merge.
_PROBLEM_FIELDS: tuple[str, ...] = (
    "accident_reported", "damage_reported", "total_loss", "structural_damage",
    "airbag_deployment", "odometer_rollback", "title_brand_problem", "open_recalls",
    "auction_problem", "insurance_loss",
)
# Fields taken from whichever vendor supplies them, first non-None wins.
_FIRST_WINS_FIELDS: tuple[str, ...] = (
    "reliability_forecast", "avg_annual_repair_cost", "autocheck_score",
    "autocheck_score_low", "autocheck_score_high", "odometer_reading",
    "service_record_count", "detailed_record_count",
)


def merge_reports(reports: list[HistoryReport]) -> HistoryReport:
    """Merge one or more vendor reports for the same vehicle, pessimistically.

    Args:
        reports: Parsed reports for a single VIN, in preference order.

    Returns:
        A single HistoryReport carrying the union of findings, with `sources` listing the vendors
        that contributed and `conflicts` naming every field where they disagreed.
    """
    parsed = [report for report in reports if report.is_parsed]
    if not parsed:
        # Prefer reporting "blocked" over "unavailable": blocked is retryable and never cached.
        blocked = next((r for r in reports if r.status == STATUS_BLOCKED), None)
        return blocked or (reports[0] if reports else
                           HistoryReport(vin="", status=STATUS_UNAVAILABLE))
    if len(parsed) == 1:
        return parsed[0]

    merged = HistoryReport(
        vin=parsed[0].vin,
        status=STATUS_PARSED,
        vendor="+".join(report.vendor or "?" for report in parsed),
        source_url=parsed[0].source_url,
        sources=[report.vendor or "?" for report in parsed],
    )

    for name in _PROBLEM_FIELDS:
        values = [(report.vendor, getattr(report, name)) for report in parsed]
        known = [(vendor, value) for vendor, value in values if value is not None]
        if not known:
            continue
        setattr(merged, name, any(value for _, value in known))
        if len({value for _, value in known}) > 1:
            detail = " ".join(f"{vendor}={value}" for vendor, value in known)
            merged.conflicts.append(f"{name}: {detail}")

    for name in _FIRST_WINS_FIELDS:
        for report in parsed:
            value = getattr(report, name)
            if value is not None:
                setattr(merged, name, value)
                break

    # Owner count: the higher estimate, and flag disagreement — both vendors call it estimated.
    owner_counts = [(r.vendor, r.owner_count) for r in parsed if r.owner_count is not None]
    if owner_counts:
        merged.owner_count = max(count for _, count in owner_counts)
        if len({count for _, count in owner_counts}) > 1:
            merged.conflicts.append(
                "owner_count: " + " ".join(f"{v}={c}" for v, c in owner_counts)
                + " (took the higher)")

    # Accident count: the higher of the two.
    accident_counts = [r.accident_count for r in parsed if r.accident_count is not None]
    if accident_counts:
        merged.accident_count = max(accident_counts)

    for report in parsed:
        for use_type in report.use_types:
            if use_type not in merged.use_types:
                merged.use_types.append(use_type)
        merged.title_brands = merged.title_brands or report.title_brands
        merged.unrecognized_sections.extend(report.unrecognized_sections)
        merged.notes.extend(report.notes)

    return merged


def has_carfax(report: HistoryReport | None) -> bool:
    """Whether a report actually carries Carfax **findings**.

    The authoritative "does this vehicle still need Carfax?" test. Three conditions, each of which
    is load-bearing:

    - `is_parsed` is required because a failed fetch still returns `vendor="carfax"`. When both
      vendors fail, merge_reports prefers the blocked report — so the merged result reads
      `vendor="carfax"`, `status="history_blocked"` and every field None. Treating that as having
      Carfax would both count it as parsed and stop the next run from retrying it.
    - `vendor` alone identifies a Carfax-only report, because `parse_carfax_text` never populates
      `sources` and merge_reports returns a single-vendor report unmodified.
    - `sources` identifies a genuine two-vendor merge, which sets `vendor="autocheck+carfax"`.
    """
    if report is None or not report.is_parsed:
        return False
    return "carfax" in (report.sources or []) or report.vendor == "carfax"


def _fetch_report_page(
    context,
    url: str,
    label: str,
    allow_manual_assist: bool,
    assist_timeout_s: int,
    on_challenge: Callable[[str, int], None] | None = None,
    abort: threading.Event | None = None,
) -> tuple[str | None, str]:
    """Open a report URL and return (visible_text, status).

    Status is `parsed` when real content loaded, `history_blocked` when an anti-bot challenge
    stood in the way, `history_unavailable` when the page failed for any other reason.

    `on_challenge` and `abort` are forwarded to wait_for_manual_assist so a GUI can raise its own
    alert and cancel the wait. Neither can turn a blocked page into a parsed one.
    """
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        human_pause(4.0, 8.0)

        if detect_challenge(page):
            if not allow_manual_assist:
                return None, STATUS_BLOCKED
            if not wait_for_manual_assist(page, looks_like_report, label,
                                          timeout_s=assist_timeout_s,
                                          on_challenge=on_challenge, abort=abort):
                return None, STATUS_BLOCKED
            human_pause(2.0, 4.0)

        text = page_text(page)
        if len(text) < 1500 or detect_challenge(page):
            # Short and/or still challenged: treat as blocked so it retries rather than being
            # cached as a permanent "unavailable".
            return (text or None), STATUS_BLOCKED
        return text, STATUS_PARSED
    except Exception as exc:
        print(f"    [history] {label}: {type(exc).__name__}: {exc}")
        return None, STATUS_UNAVAILABLE
    finally:
        page.close()


def fetch_autocheck(context, browser_page, listing: Listing) -> HistoryReport:
    """Fetch and parse the AutoCheck report for one vehicle."""
    report_url = fetch_autocheck_report_url(browser_page, listing.vehicle_id)
    if not report_url:
        return HistoryReport(vin=listing.vin, status=STATUS_UNAVAILABLE, vendor="autocheck",
                             notes=["no AutoCheck report iframe on the wrapper page"])

    text, status = _fetch_report_page(
        context, report_url, f"AutoCheck {listing.vin}",
        allow_manual_assist=False, assist_timeout_s=0)
    if status != STATUS_PARSED or not text:
        return HistoryReport(vin=listing.vin, status=status, vendor="autocheck",
                             source_url=report_url)

    archive_raw(listing.vin, text, extension="autocheck.txt")
    return parse_autocheck_text(text, vin=listing.vin, source_url=report_url)


def fetch_carfax(
    context,
    listing: Listing,
    allow_manual_assist: bool = True,
    assist_timeout_s: int = 240,
    on_challenge: Callable[[str, int], None] | None = None,
    abort: threading.Event | None = None,
) -> HistoryReport:
    """Fetch and parse the Carfax report for one vehicle.

    The URL is built from the VIN alone — Carvana's link is not tokenized, so no VDP visit is
    needed. This is the fetch that DataDome guards.
    """
    url = listing.carfax_url
    text, status = _fetch_report_page(
        context, url, f"Carfax {listing.label} — {listing.vin}",
        allow_manual_assist=allow_manual_assist, assist_timeout_s=assist_timeout_s,
        on_challenge=on_challenge, abort=abort)
    if status != STATUS_PARSED or not text:
        return HistoryReport(vin=listing.vin, status=status, vendor="carfax", source_url=url)

    archive_raw(listing.vin, text, extension="carfax.txt")
    return parse_carfax_text(text, vin=listing.vin, source_url=url)


def get_or_fetch(
    context,
    browser_page,
    listing: Listing,
    connection,
    want_carfax: bool,
    allow_manual_assist: bool = True,
    assist_timeout_s: int = 240,
    verbose: bool = True,
    on_challenge: Callable[[str, int], None] | None = None,
    abort: threading.Event | None = None,
) -> tuple[HistoryReport, bool]:
    """Return a merged history report for one vehicle, using the cache where possible.

    Args:
        context: Active browser context (used for report tabs).
        browser_page: Reusable page for Carvana-domain navigation.
        listing: The vehicle.
        connection: Open cache connection.
        want_carfax: Whether to attempt the rate-limited Carfax fetch for this vehicle.
        allow_manual_assist: Whether to pause for a human to solve a puzzle.
        assist_timeout_s: How long to wait for that.
        verbose: Print progress.
        on_challenge: Forwarded to the manual-assist wait so a GUI can raise its own alert.
        abort: Forwarded to the manual-assist wait so a GUI can cancel it.

    Returns:
        (report, from_cache).
    """
    cached = get_history(connection, listing.vin)
    if cached and cached.get("payload"):
        report = HistoryReport.from_dict(cached["payload"])
        if not want_carfax or has_carfax(report):
            if verbose:
                print(f"    [history] {listing.vin} from cache "
                      f"({report.vendor}, {cached['age_days']}d old)")
            return report, True

        # Cached AutoCheck but Carfax now wanted: fetch only the missing vendor and merge onto
        # what we already have, rather than paying for the AutoCheck fetch twice.
        if verbose:
            print(f"    [history] {listing.vin} cached AutoCheck — fetching Carfax only")
        carfax_only = fetch_carfax(context, listing, allow_manual_assist, assist_timeout_s,
                                   on_challenge=on_challenge, abort=abort)
        if verbose:
            print(f"    [history] carfax:    {carfax_only.status}")
        merged_with_cache = merge_reports([report, carfax_only])
        if merged_with_cache.conflicts and verbose:
            for conflict in merged_with_cache.conflicts:
                print(f"    [history] CONFLICT {conflict}")
        put_history(connection, listing.vin, merged_with_cache.status,
                    vendor=merged_with_cache.vendor, payload=merged_with_cache.to_dict(),
                    source_url=merged_with_cache.source_url)
        return merged_with_cache, False

    collected: list[HistoryReport] = []

    autocheck_report = fetch_autocheck(context, browser_page, listing)
    collected.append(autocheck_report)
    if verbose:
        print(f"    [history] autocheck: {autocheck_report.status}"
              + (f" score={autocheck_report.autocheck_score}"
                 if autocheck_report.autocheck_score else ""))

    if want_carfax:
        human_pause()
        carfax_report = fetch_carfax(context, listing, allow_manual_assist, assist_timeout_s,
                                     on_challenge=on_challenge, abort=abort)
        collected.append(carfax_report)
        if verbose:
            print(f"    [history] carfax:    {carfax_report.status}"
                  + (f" owners={carfax_report.owner_count} "
                     f"accidents={carfax_report.accident_count}"
                     if carfax_report.is_parsed else ""))

    merged = merge_reports(collected)
    if merged.conflicts and verbose:
        for conflict in merged.conflicts:
            print(f"    [history] CONFLICT {conflict}")

    put_history(connection, listing.vin, merged.status, vendor=merged.vendor,
                payload=merged.to_dict(), source_url=merged.source_url)
    return merged, False
