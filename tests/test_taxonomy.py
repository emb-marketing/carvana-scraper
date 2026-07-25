"""Tests for the Carvana taxonomy extractor.

Fully offline: every fixture is an inline flight-stream document built in the test itself, so
these run without the 2.1 MB recon capture (which is gitignored and machine-local).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.extract_taxonomy import (  # noqa: E402
    TaxonomyShapeError,
    extract_taxonomy,
)


def flight_page(payload: dict) -> str:
    """Wrap a payload as a Carvana RSC flight-stream HTML document.

    Mirrors the real emission shape: `self.__next_f.push([1,"<json-escaped chunk>"])`. The double
    json.dumps is deliberate — the inner call serializes the payload, the outer one escapes it into
    a JavaScript string literal exactly as Next.js does.
    """
    chunk = json.dumps(json.dumps(payload))
    return f"<html><body><script>self.__next_f.push([1,{chunk}])</script></body></html>"


def make_facets(makes: dict, **ranges) -> dict:
    """Build a minimal facet envelope with the given makes tree and Range facets."""
    facets: dict = {"makes": makes}
    for name, (low, high) in ranges.items():
        facets[name] = {"type": "Range", "key": name, "min": low, "max": high}
    return facets


TOYOTA_TREE = {
    "Toyota": {
        "count": 6223,
        "key": "Toyota",
        "parentModels": [
            {"key": "4Runner", "count": 367, "trims": [{"key": "SR5"}, {"key": "TRD Pro"}]},
            {"key": "Grand Highlander", "count": 69, "trims": [{"key": "Limited"}]},
        ],
    },
    "Ford": {
        "count": 7325,
        "key": "Ford",
        "parentModels": [{"key": "F-150", "count": 900, "trims": [{"key": "XLT"}]}],
    },
}


class ExtractTaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        page = flight_page(make_facets(
            TOYOTA_TREE, year=(2009, 2027), price=(6990, 260990), mileage=(4, 147743)
        ))
        self.document = extract_taxonomy(page, source="test")

    def test_makes_are_sorted_case_insensitively(self) -> None:
        self.assertEqual([make["name"] for make in self.document["makes"]], ["Ford", "Toyota"])

    def test_models_come_from_parent_models_with_counts(self) -> None:
        toyota = next(m for m in self.document["makes"] if m["name"] == "Toyota")
        self.assertEqual(toyota["count"], 6223)
        self.assertEqual(
            [(model["name"], model["count"]) for model in toyota["models"]],
            [("4Runner", 367), ("Grand Highlander", 69)],
        )

    def test_slug_matches_the_scrapers_own_url_builder(self) -> None:
        """The slug must be what search.build_search_url would produce, not a hand-written guess.

        If these drift apart the GUI sends the user to a URL the CLI would never generate.
        """
        from carvana_scraper.models import SearchCriteria
        from carvana_scraper.search import build_search_url

        toyota = next(m for m in self.document["makes"] if m["name"] == "Toyota")
        for model in toyota["models"]:
            expected = build_search_url(SearchCriteria(make="Toyota", model=model["name"]))
            self.assertTrue(
                expected.endswith("/" + model["slug"]),
                f"{model['name']}: slug {model['slug']!r} does not match {expected!r}",
            )

    def test_multiword_model_slug(self) -> None:
        toyota = next(m for m in self.document["makes"] if m["name"] == "Toyota")
        grand = next(m for m in toyota["models"] if m["name"] == "Grand Highlander")
        self.assertEqual(grand["slug"], "toyota-grand-highlander")

    def test_trims_are_deduplicated_and_sorted(self) -> None:
        toyota = next(m for m in self.document["makes"] if m["name"] == "Toyota")
        runner = next(m for m in toyota["models"] if m["name"] == "4Runner")
        self.assertEqual(runner["trims"], ["SR5", "TRD Pro"])

    def test_bounds_are_captured(self) -> None:
        self.assertEqual(
            self.document["bounds"],
            {"year": [2009, 2027], "price": [6990, 260990], "mileage": [4, 147743]},
        )

    def test_extracted_at_and_source_recorded(self) -> None:
        self.assertEqual(self.document["source"], "test")
        self.assertRegex(self.document["extracted_at"], r"^\d{4}-\d{2}-\d{2}$")


class FacetSelectionTests(unittest.TestCase):
    def test_picks_the_fullest_tree_when_several_are_present(self) -> None:
        """A /cars page emits the facet envelope more than once; the copies are not equivalent.

        On a filtered page every make but the applied one has an empty model list. Picking by
        emission order would silently ship a taxonomy with one model in it.
        """
        pruned = {"Toyota": {"count": 3812, "key": "Toyota", "parentModels": []}}
        page = (
            flight_page(make_facets(pruned))
            + flight_page(make_facets(TOYOTA_TREE))
        )
        document = extract_taxonomy(page)
        toyota = next(m for m in document["makes"] if m["name"] == "Toyota")
        self.assertEqual(len(toyota["models"]), 2)


class FailureModeTests(unittest.TestCase):
    def test_no_flight_stream_raises(self) -> None:
        with self.assertRaises(TaxonomyShapeError) as caught:
            extract_taxonomy("<html><body>nothing here</body></html>")
        self.assertIn("No RSC flight chunks", str(caught.exception))

    def test_facet_tree_without_parent_models_raises(self) -> None:
        """An empty tree must fail loudly rather than write a taxonomy with no models.

        Silently succeeding here would give the GUI a Make dropdown whose Model list is always
        empty — a failure the user would read as "Carvana has no inventory".
        """
        page = flight_page(make_facets({"Toyota": {"count": 1, "key": "Toyota",
                                                   "parentModels": []}}))
        with self.assertRaises(TaxonomyShapeError) as caught:
            extract_taxonomy(page)
        self.assertIn("none carried any parent models", str(caught.exception))

    def test_malformed_range_facet_is_skipped_not_defaulted(self) -> None:
        """A bad bound must be omitted, never invented.

        An absent bound leaves the input unbounded, which is merely permissive. An invented one
        would silently reject prices the user legitimately wants to search.
        """
        facets = make_facets(TOYOTA_TREE, year=(2009, 2027))
        facets["price"] = {"type": "Range", "key": "price", "min": None, "max": 260990}
        facets["mileage"] = {"type": "Range", "key": "mileage", "min": 900, "max": 4}  # inverted
        document = extract_taxonomy(flight_page(facets))
        self.assertEqual(document["bounds"], {"year": [2009, 2027]})


class CommittedTaxonomyTests(unittest.TestCase):
    """Sanity-check the committed artifact itself, so a bad regeneration cannot land quietly."""

    def setUp(self) -> None:
        path = PROJECT_ROOT / "config" / "carvana-taxonomy.json"
        if not path.exists():
            self.skipTest("config/carvana-taxonomy.json not generated yet")
        self.document = json.loads(path.read_text(encoding="utf-8"))

    def test_has_a_plausible_number_of_makes_and_models(self) -> None:
        makes = self.document["makes"]
        models = sum(len(make["models"]) for make in makes)
        self.assertGreaterEqual(len(makes), 30, "far fewer makes than Carvana lists")
        self.assertGreaterEqual(models, 400, "far fewer models than Carvana lists")

    def test_every_model_has_a_nonempty_slug(self) -> None:
        for make in self.document["makes"]:
            for model in make["models"]:
                self.assertTrue(model["slug"], f"{make['name']} {model['name']} has no slug")
                self.assertNotIn(" ", model["slug"])

    def test_bounds_present_for_the_fields_the_ui_binds(self) -> None:
        for name in ("year", "price", "mileage"):
            self.assertIn(name, self.document["bounds"])


if __name__ == "__main__":
    unittest.main()
