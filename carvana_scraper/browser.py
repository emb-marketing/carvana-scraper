"""Dedicated headful real-Chrome session, human pacing, and manual-assist on challenges.

Mirrors the persistent-profile pattern proven in ~/projects/ioverlander-kml — a separate Chrome
profile at .browser-profile/ so an anti-bot flag can never affect day-to-day browsing, and so
trust cookies earned on one run carry into the next.

This module never attempts to solve a bot challenge. It detects one, alerts the operator, and
waits for a human to clear it.
"""

from __future__ import annotations

import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / ".browser-profile"

# Substrings that indicate an anti-bot interstitial rather than real content. Matched against
# lowercased HTML **and** visible text — the HTML half is load-bearing, see below.
#
# The observed blocker on Carfax is DataDome, confirmed 2026-07-25: after ~6 report fetches in a
# session it serves a 1541-byte shell whose entire content is a cross-origin puzzle iframe:
#
#     <title>carfax.com</title>
#     <iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=…&hash=…&cid=…">
#     <script src="https://ct.captcha-delivery.com/c.js">
#
# Because the puzzle lives in that iframe, `document.body.innerText` of the top document is
# EMPTY. A text-only detector sees a blank page and wrongly concludes "parse failure" rather
# than "solvable challenge" — which is exactly the bug this list now fixes. Cloudflare, Imperva,
# Akamai and PerimeterX markers are kept for the day a vendor changes.
CHALLENGE_MARKERS: tuple[str, ...] = (
    # DataDome — the vendor actually in front of Carfax.
    "captcha-delivery.com",
    "datadome",
    "geo.captcha-delivery",
    # Cloudflare.
    "verify you are human",
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "cf_chl",
    "challenge-platform",
    "turnstile",
    "attention required",
    # Imperva / Incapsula.
    "_incapsula_",
    "incident id",
    "imperva",
    # Akamai / PerimeterX / generic.
    "pardon our interruption",
    "access denied",
    "unusual traffic",
    "enable javascript and cookies",
    "are you a robot",
    "px-captcha",
    "perimeterx",
    "please verify you are a human",
    "request unsuccessful",
    "hcaptcha",
    "recaptcha",
    "arkose",
    "funcaptcha",
    "geetest",
)

# A page this small with almost no visible text is an interstitial of some kind even when no
# known marker matches — a backstop so a new vendor degrades to "blocked" (retry later) rather
# than "unavailable" (cached as fact).
_SUSPICIOUS_HTML_BYTES = 6000
_SUSPICIOUS_TEXT_BYTES = 250

# Chrome flags: suppress the automation banner and the first-run interstitials that would
# otherwise block a fresh profile.
CHROME_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


class ProfileLockedError(RuntimeError):
    """Raised when the dedicated Chrome profile is already in use by another process."""


def human_pause(min_s: float = 4.0, max_s: float = 12.0) -> None:
    """Sleep a randomized, human-scale interval.

    Every navigation in this project goes through here. Randomized rather than fixed so the
    request cadence does not read as a metronome.
    """
    time.sleep(random.uniform(min_s, max_s))


def _assert_profile_unlocked(profile_dir: Path) -> None:
    """Fail early and legibly if a previous run's Chrome still holds the profile.

    Playwright's own error for this case is opaque, and the fix (close the other window) is not
    guessable from it.
    """
    lock = profile_dir / "SingletonLock"
    if lock.exists() or lock.is_symlink():
        raise ProfileLockedError(
            f"The dedicated Chrome profile at {profile_dir} appears to be in use "
            f"({lock.name} present).\n"
            "This tool launches its OWN Chrome window, separate from your everyday browser. "
            "Close the Chrome window from the previous run and try again.\n"
            "If no such window is open, the lock is stale — delete it and retry:\n"
            f"    rm -f '{lock}'"
        )


@contextmanager
def session(
    profile_dir: Path | str = DEFAULT_PROFILE_DIR,
    headless: bool = False,
    viewport_width: int = 1440,
    viewport_height: int = 900,
) -> Iterator[BrowserContext]:
    """Yield a persistent real-Chrome browser context.

    Uses channel="chrome" (the actual Google Chrome install) rather than Playwright's bundled
    Chromium: a real browser binary with a real, aging profile is far less likely to be
    challenged than a fresh headless Chromium.

    Args:
        profile_dir: Directory holding the dedicated Chrome profile. Created if absent.
        headless: Left configurable for tests only. Real runs are headful by design — a human
            must be able to see and clear a challenge.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.

    Yields:
        A Playwright BrowserContext whose cookies and storage persist across runs.

    Raises:
        ProfileLockedError: If another Chrome process already holds the profile.
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    _assert_profile_unlocked(profile_dir)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": viewport_width, "height": viewport_height},
            args=CHROME_ARGS,
        )
        try:
            yield context
        finally:
            context.close()


def page_text(page: Page) -> str:
    """Return the page's visible text, or an empty string if the body is not available."""
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def detect_challenge(page: Page) -> list[str]:
    """Return the challenge markers present on the page, empty if it looks like real content.

    Checked against both HTML and visible text, because DataDome's puzzle lives in a cross-origin
    iframe and leaves the top document's text empty — see CHALLENGE_MARKERS. Falls back to a
    size heuristic so an unrecognized vendor is still reported as a challenge rather than
    mistaken for real (but empty) content.
    """
    try:
        html = page.content()
    except Exception:
        return []
    text = page_text(page)
    haystack = (html + " " + text).lower()

    markers = [marker for marker in CHALLENGE_MARKERS if marker in haystack]
    if markers:
        return markers

    if len(html) < _SUSPICIOUS_HTML_BYTES and len(text) < _SUSPICIOUS_TEXT_BYTES:
        return [f"suspiciously-empty-page(html={len(html)}b,text={len(text)}b)"]
    return []


def wait_for_manual_assist(
    page: Page,
    is_ready: Callable[[Page], bool],
    label: str,
    timeout_s: int = 180,
    poll_s: float = 3.0,
) -> bool:
    """Alert the operator to a bot challenge and wait for them to clear it.

    Deliberately does not attempt to solve the challenge — a human check is answered by a human.

    Args:
        page: The challenged page. Its window is already visible to the operator.
        is_ready: Predicate returning True once real content has replaced the interstitial.
        label: Human-readable identifier (a VIN or URL) for the alert banner.
        timeout_s: How long to wait before giving up.
        poll_s: Seconds between readiness checks.

    Returns:
        True if the page became ready, False on timeout. Never raises, and never returns True
        speculatively — the caller must treat False as "this vehicle's history is unknown",
        not as "the page loaded".
    """
    bell = "\a"
    banner = "=" * 72
    print(
        f"\n{bell}{banner}\n"
        f"  BOT CHALLENGE — manual assist needed\n"
        f"  {label}\n"
        f"{banner}\n"
        f"  Solve the challenge in the Chrome window that is already open.\n"
        f"  Waiting up to {timeout_s}s; this run continues automatically once it clears.\n"
        f"{banner}",
        flush=True,
    )

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            if is_ready(page):
                remaining = int(deadline - time.monotonic())
                print(f"  Challenge cleared — continuing ({remaining}s of budget unused).",
                      flush=True)
                return True
        except Exception:
            continue  # navigation mid-check; try again on the next poll

    print(f"  Timed out after {timeout_s}s — recording as blocked, not as clean.\n",
          file=sys.stderr, flush=True)
    return False


def login(profile_dir: Path | str = DEFAULT_PROFILE_DIR) -> int:
    """One-time setup of the dedicated Chrome profile.

    Interactive by design — the one command that waits on the operator. Two things happen here,
    and both persist in the profile for every later run:

    1. **Trust.** Browsing the site briefly gives the profile a history and cookies, which is
       most of why later report fetches are not challenged immediately.
    2. **Delivery location.** `--zip` cannot set Carvana's pricing zip; Carvana derives it from
       the session and defaults to its own guess. Setting the delivery zip in the UI **here** is
       what makes shipping costs — and therefore landed prices and the ranking — correct. If it
       is not set, every run prints a warning naming the zip Carvana actually priced against.

    Returns:
        A process exit code.
    """
    with session(profile_dir) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.carvana.com/cars", wait_until="domcontentloaded", timeout=90_000)
        banner = "=" * 72
        print(
            f"\n{banner}\n"
            "  ONE-TIME SETUP — do both of these in the Chrome window that just opened\n"
            f"{banner}\n"
            "  1. Accept any cookie prompt and browse a couple of pages (builds trust).\n"
            "  2. IMPORTANT: set your delivery ZIP using Carvana's location picker\n"
            "     (the delivery/location control in the header). Shipping cost depends on it,\n"
            "     and it cannot be set from the command line — only here, once.\n"
            f"{banner}\n"
            f"  cookies currently in profile: {len(context.cookies())}\n\n"
            "  Press Enter here when done to save the session and close.",
            flush=True,
        )
        input()
        print(f"  cookies after session: {len(context.cookies())}")
        print("  Profile saved. Later runs will warn if Carvana still prices against a "
              "different zip.")
    return 0


def looks_like_report(page: Page, min_text_bytes: int = 2000) -> bool:
    """Heuristic readiness check for a vehicle-history report page.

    A real report is text-heavy and free of challenge markers; an interstitial is neither.
    Used as the `is_ready` predicate for wait_for_manual_assist.
    """
    if detect_challenge(page):
        return False
    return len(page_text(page)) >= min_text_bytes


def vin_from_carfax_url(url: str) -> str | None:
    """Extract the VIN from a Carvana-issued Carfax report URL."""
    match = re.search(r"[?&]vin=([A-HJ-NPR-Z0-9]{17})", url, re.I)
    return match.group(1).upper() if match else None
