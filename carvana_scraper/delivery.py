"""Persist and replay the delivery location Carvana prices against.

**This corrects an earlier finding.** The tool used to document that `--zip` could not influence
Carvana's pricing zip. That was wrong in an interesting way, established 2026-07-25 over five
observations:

| cookies present before the first request | zip Carvana priced with |
|---|---|
| none                                     | 89101 (IP geolocation)  |
| `CVCurrentZip` only                      | 89101                   |
| `CVCurrentZip` + `Source` + `AccuracyRadius` | 89101               |
| `CVCurrentZip` + `CVCurrentCity` + `CVCurrentState` | **as asked**  |
| the same three, asking a different zip (control)    | **as asked**  |

Two facts explain the original conclusion. `CVCurrentZip` is **session-scoped** — absent at the
start of every new browser session, so Carvana re-geolocates from the IP every run and a location
set in its picker never survives. And a *partial* location is discarded: Carvana wants zip, city
and state together or it falls back to the IP.

So the location is settable, but only with a complete triple — and the city for an arbitrary zip is
not something to invent. Hence capture rather than construct: during login the operator sets the
picker, Carvana writes the triple itself, and it is saved here and replayed at the start of every
later session. Nothing in this file is guessed; every value came from Carvana.

Stored next to the browser profile because it is a property of that profile, and gitignored with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCATION_PATH = PROJECT_ROOT / ".browser-profile" / "delivery-location.json"

COOKIE_DOMAIN = ".carvana.com"

# The three Carvana actually requires. Verified by bisection — adding CVCurrentSource or
# CVCurrentAccuracyRadius changes nothing, and omitting city or state falls back to the IP.
REQUIRED_COOKIES: tuple[str, ...] = ("CVCurrentZip", "CVCurrentCity", "CVCurrentState")


def capture(context) -> dict[str, str] | None:
    """Read the delivery location Carvana has set on this session.

    Returns:
        `{"CVCurrentZip": …, "CVCurrentCity": …, "CVCurrentState": …}`, or None if Carvana has not
        written a complete location — in which case there is nothing worth saving and the caller
        should say so rather than persist a partial one.
    """
    try:
        cookies = {c["name"]: c["value"] for c in context.cookies()}
    except Exception:
        return None
    captured = {name: cookies[name] for name in REQUIRED_COOKIES
                if cookies.get(name)}
    return captured if len(captured) == len(REQUIRED_COOKIES) else None


def save(location: dict[str, str], path: Path | str = DEFAULT_LOCATION_PATH) -> Path:
    """Persist a captured location."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(location, indent=2) + "\n", encoding="utf-8")
    return target


def load(path: Path | str = DEFAULT_LOCATION_PATH) -> dict[str, str] | None:
    """Load the saved location, or None if absent or incomplete.

    Never raises: a corrupt file should degrade to "no saved location" — the run still works, it
    just prices against the IP and warns, exactly as before this existed.
    """
    target = Path(path)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    location = {name: str(data[name]) for name in REQUIRED_COOKIES if data.get(name)}
    return location if len(location) == len(REQUIRED_COOKIES) else None


def as_cookies(location: dict[str, str]) -> list[dict[str, Any]]:
    """Render a location as Playwright cookies."""
    return [{"name": name, "value": value, "domain": COOKIE_DOMAIN, "path": "/",
             "secure": True, "sameSite": "Lax"}
            for name, value in location.items()]


def apply(context, path: Path | str = DEFAULT_LOCATION_PATH) -> dict[str, str] | None:
    """Replay the saved location onto a fresh session, before any navigation.

    Must run before the first request: Carvana decides the location on the first page load and
    rewrites these cookies afterwards, so setting them later has no effect on pricing.

    Returns the location applied, or None if there was nothing saved.
    """
    location = load(path)
    if not location:
        return None
    try:
        context.add_cookies(as_cookies(location))
    except Exception:
        return None
    return location


def describe(location: dict[str, str] | None) -> str:
    """One-line human summary."""
    if not location:
        return "no saved delivery location — Carvana will price against this connection's IP"
    return (f"{location['CVCurrentZip']} "
            f"({location['CVCurrentCity']}, {location['CVCurrentState']})")
