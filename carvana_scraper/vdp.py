"""Stage 2 — per-vehicle detail fetches, all on Carvana's own unprotected domains.

Two things are needed per vehicle that the search payload does not carry:

1. **The AutoCheck report URL.** The report lives at
   `apik.carvana.io/merch/merchui/api/v1/autocheck?vehicleId=<id>&token=<token>`, and the token is
   only obtainable from the wrapper page at `/vehicle/autocheck/<vehicleId>`. So: load the wrapper,
   read the iframe `src`, hand it to history.py.
2. **Cosmetic imperfections.** Carvana photographs and describes every scuff, dent and chip in
   `spinnerdata.carvana.io/spinnerdata/<stockNumber>/spinnerData.json` — a plain CDN GET with no
   auth and no protection.

Neither source showed any rate limiting across recon. The Carfax URL needs only the VIN, so no VDP
visit is required to obtain it.
"""

from __future__ import annotations

import json
import re

from .browser import human_pause

SPINNER_DATA_URL = "https://spinnerdata.carvana.io/spinnerdata/{stock_number}/spinnerData.json"
AUTOCHECK_WRAPPER_URL = "https://www.carvana.com/vehicle/autocheck/{vehicle_id}"

# The report iframe on the wrapper page. Matched against raw HTML, so entities are still encoded.
_AUTOCHECK_IFRAME_RE = re.compile(
    r'<iframe[^>]+src="(https://apik\.carvana\.io/merch/merchui/api/v1/autocheck[^"]+)"', re.I)


def fetch_autocheck_report_url(browser_page, vehicle_id: int, verbose: bool = True) -> str | None:
    """Resolve the tokenized AutoCheck report URL for a vehicle.

    Args:
        browser_page: A Playwright Page from an active session.
        vehicle_id: Carvana's internal vehicle id (present on every search record).
        verbose: Print a line on failure.

    Returns:
        The fully qualified report URL, or None if the wrapper page carried no report iframe
        (which legitimately happens for some vehicles).
    """
    wrapper = AUTOCHECK_WRAPPER_URL.format(vehicle_id=vehicle_id)
    try:
        browser_page.goto(wrapper, wait_until="domcontentloaded", timeout=90_000)
        human_pause(2.0, 4.0)
        match = _AUTOCHECK_IFRAME_RE.search(browser_page.content())
    except Exception as exc:
        if verbose:
            print(f"    [vdp] autocheck wrapper failed for {vehicle_id}: "
                  f"{type(exc).__name__}: {exc}")
        return None

    if not match:
        if verbose:
            print(f"    [vdp] no AutoCheck iframe on wrapper for vehicle {vehicle_id}")
        return None
    # The src is HTML-escaped in the document; the token contains '+' and '=' which must survive.
    return match.group(1).replace("&amp;", "&")


def fetch_imperfections(
    browser_page,
    stock_number: int | None,
    verbose: bool = True,
) -> list[dict] | None:
    """Fetch Carvana's photographed cosmetic imperfections for a vehicle.

    Uses the browser's own request context so the call carries session cookies and headers rather
    than looking like an unrelated HTTP client.

    Args:
        browser_page: A Playwright Page from an active session.
        stock_number: Carvana stock number from the search record.
        verbose: Print a line on failure.

    Returns:
        A list of imperfection dicts (title, description, location, zoneDescription), an empty
        list when the vehicle genuinely has none, or None when the lookup could not be performed.
    """
    if not stock_number:
        return None
    url = SPINNER_DATA_URL.format(stock_number=stock_number)
    try:
        response = browser_page.request.get(url, timeout=45_000)
        if not response.ok:
            if verbose:
                print(f"    [vdp] spinnerData {response.status} for stock {stock_number}")
            return None
        payload = json.loads(response.text())
    except Exception as exc:
        if verbose:
            print(f"    [vdp] spinnerData failed for {stock_number}: "
                  f"{type(exc).__name__}: {exc}")
        return None

    imperfections = payload.get("imperfections")
    if imperfections is None:
        return None
    return [
        {
            "title": item.get("title"),
            "description": item.get("description"),
            "location": item.get("location"),
            "zone": item.get("zoneDescription"),
        }
        for item in imperfections
        if isinstance(item, dict)
    ]


def summarize_imperfections(imperfections: list[dict] | None) -> str:
    """Compact human summary, e.g. "9: Water Spots(Roof), Scratch(Driver Door), …"."""
    if imperfections is None:
        return "unknown"
    if not imperfections:
        return "none reported"
    parts = [
        f"{item.get('title')}" + (f"({item['zone']})" if item.get("zone") else "")
        for item in imperfections[:4]
    ]
    suffix = f", +{len(imperfections) - 4} more" if len(imperfections) > 4 else ""
    return f"{len(imperfections)}: " + ", ".join(parts) + suffix
