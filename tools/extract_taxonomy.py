#!/usr/bin/env python3
"""Extract Carvana's make/model taxonomy into config/carvana-taxonomy.json.

The unfiltered search results page (`https://www.carvana.com/cars`) server-renders Carvana's
entire filter facet tree inside its RSC flight stream: 40 makes, 528 parent models, and the real
numeric bounds of current inventory. The GUI's dropdowns are built from that, so the app never has
to guess what Carvana actually sells.

Two ways to run this:

    python3 tools/extract_taxonomy.py                        # from the saved recon fixture
    python3 tools/extract_taxonomy.py path/to/search.html    # from any saved /cars page

The default source, `fixtures/recon/search-base.html`, is excluded by .gitignore's whitelist — it
exists only on the machine that captured it. That is precisely why the extracted JSON is committed:
without it a fresh clone would have no dropdown data at all. The app can refresh it later by
re-fetching /cars through the live browser session.

Deliberately reuses `rsc.decode_flight_stream` rather than re-implementing chunk decoding, so
there is exactly one place that understands Carvana's flight-stream format.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

# Allow `python3 tools/extract_taxonomy.py` from the repo root without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carvana_scraper.rsc import decode_flight_stream, extract_objects_with_key  # noqa: E402
from carvana_scraper.search import slugify  # noqa: E402

DEFAULT_SOURCE = PROJECT_ROOT / "fixtures" / "recon" / "search-base.html"
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "carvana-taxonomy.json"

# Range facets worth surfacing as input bounds in the UI. Carvana reports these as global
# inventory bounds, not scoped to the current filter, so they are stable enough to ship.
_RANGE_FACETS: tuple[str, ...] = ("year", "price", "mileage")


class TaxonomyShapeError(RuntimeError):
    """Raised when the page yields no usable facet tree.

    A hard failure by design: silently writing a taxonomy with three makes in it would produce a
    GUI whose dropdowns quietly omit most of Carvana's inventory.
    """


def _select_facet_object(candidates: list[dict]) -> dict:
    """Pick the facet object carrying the fullest make tree.

    A /cars page embeds the facet envelope more than once, and the copies are not equivalent: on a
    filtered page every make except the applied one has an empty model list, and even on the
    unfiltered page one copy carries no models at all. Choosing by "most parent models" gets the
    complete tree without depending on emission order.
    """
    best: dict | None = None
    best_count = -1
    for candidate in candidates:
        makes = candidate.get("makes")
        if not isinstance(makes, dict):
            continue
        count = sum(
            len(entry.get("parentModels") or [])
            for entry in makes.values()
            if isinstance(entry, dict)
        )
        if count > best_count:
            best, best_count = candidate, count

    if best is None or best_count == 0:
        raise TaxonomyShapeError(
            f"Found {len(candidates)} facet object(s) but none carried any parent models. "
            "Carvana's facet shape changed, or this HTML is not a /cars search page — "
            "re-run docs/RECON.md discovery before trusting the result."
        )
    return best


def _extract_bounds(facets: dict) -> dict[str, list[int]]:
    """Pull the numeric Range facets into `{name: [min, max]}`.

    Missing or malformed facets are skipped rather than defaulted: an absent bound makes the UI
    field unbounded, which is merely permissive. A wrong invented bound would silently reject
    legitimate input.
    """
    bounds: dict[str, list[int]] = {}
    for name in _RANGE_FACETS:
        facet = facets.get(name)
        if not isinstance(facet, dict):
            continue
        low, high = facet.get("min"), facet.get("max")
        if isinstance(low, int) and isinstance(high, int) and low <= high:
            bounds[name] = [low, high]
    return bounds


def extract_taxonomy(html: str, source: str = "") -> dict:
    """Build the taxonomy document from a Carvana /cars page.

    Args:
        html: Full HTML of a Carvana search results page.
        source: Provenance string recorded in the output, e.g. the fixture path or a URL.

    Returns:
        The taxonomy document: `extracted_at`, `source`, `bounds`, `makes`.

    Raises:
        TaxonomyShapeError: If no facet tree carrying parent models is present.
    """
    stream = decode_flight_stream(html)
    if not stream:
        raise TaxonomyShapeError(
            f"No RSC flight chunks in the page (html={len(html)} bytes). "
            "Expected `self.__next_f.push([1,\"…\"])` — Carvana may have changed rendering."
        )

    facets = _select_facet_object(extract_objects_with_key(stream, "makes"))
    raw_makes: dict = facets["makes"]

    makes: list[dict] = []
    for make_name, entry in sorted(raw_makes.items(), key=lambda pair: pair[0].lower()):
        if not isinstance(entry, dict):
            continue
        models: list[dict] = []
        for parent in entry.get("parentModels") or []:
            model_name = parent.get("key")
            if not model_name:
                continue
            models.append({
                "name": model_name,
                "count": parent.get("count") or 0,
                # Built with the scraper's own slugify so the app and the CLI can never disagree
                # about which URL a given make/model pair resolves to. Known mismatches with
                # Carvana's SEO links (e.g. F-150) are deliberately NOT corrected here — see the
                # module docstring of search.py and the note in README.
                "slug": f"{slugify(make_name)}-{slugify(model_name)}",
                "trims": sorted(
                    {
                        trim.get("key")
                        for trim in parent.get("trims") or []
                        if isinstance(trim, dict) and trim.get("key")
                    }
                ),
            })
        makes.append({
            "name": make_name,
            "count": entry.get("count") or 0,
            "models": sorted(models, key=lambda model: model["name"].lower()),
        })

    return {
        "extracted_at": date.today().isoformat(),
        "source": source,
        "bounds": _extract_bounds(facets),
        "makes": makes,
    }


def write_taxonomy(document: dict, output_path: Path | str = DEFAULT_OUTPUT) -> Path:
    """Write the taxonomy document as formatted JSON and return the path written."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    """Extract from the given (or default) HTML file and write the taxonomy JSON."""
    source_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_SOURCE
    if not source_path.exists():
        print(
            f"source not found: {source_path}\n"
            "The default source is a gitignored recon capture that exists only on the machine "
            "that made it. Save a copy of https://www.carvana.com/cars and pass its path, or use "
            "the app's 'Refresh taxonomy' button.",
            file=sys.stderr,
        )
        return 2

    html = source_path.read_text(encoding="utf-8", errors="replace")
    try:
        document = extract_taxonomy(html, source=str(source_path.relative_to(PROJECT_ROOT)))
    except (TaxonomyShapeError, ValueError) as exc:
        print(f"extraction failed: {exc}", file=sys.stderr)
        return 1

    output = write_taxonomy(document)
    model_count = sum(len(make["models"]) for make in document["makes"])
    trim_count = sum(len(model["trims"]) for make in document["makes"] for model in make["models"])
    print(
        f"wrote {output.relative_to(PROJECT_ROOT)}: "
        f"{len(document['makes'])} makes, {model_count} models, {trim_count} trims"
    )
    print(f"bounds: {document['bounds']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
