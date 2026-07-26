"""Tests for capturing and replaying the delivery location.

Offline. The empirical basis for this module is recorded in its docstring: five live observations
established that Carvana honours a COMPLETE location (zip + city + state) and discards a partial
one, and that the zip cookie is session-scoped so it must be replayed every session.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carvana_scraper import delivery

COMPLETE = {"CVCurrentZip": "89002", "CVCurrentCity": "Henderson", "CVCurrentState": "NV"}


class _FakeContext:
    """Minimal stand-in for a Playwright context."""

    def __init__(self, cookies=None, raise_on_cookies=False, raise_on_add=False):
        self._cookies = cookies or []
        self.added: list[dict] = []
        self.raise_on_cookies = raise_on_cookies
        self.raise_on_add = raise_on_add

    def cookies(self):
        if self.raise_on_cookies:
            raise RuntimeError("context closed")
        return list(self._cookies)

    def add_cookies(self, cookies):
        if self.raise_on_add:
            raise RuntimeError("context closed")
        self.added.extend(cookies)

    def close(self):
        """session() closes the context on exit."""


def cookie_list(**values):
    return [{"name": name, "value": value} for name, value in values.items()]


class CaptureTests(unittest.TestCase):
    def test_captures_a_complete_location(self) -> None:
        context = _FakeContext(cookie_list(**COMPLETE, CVCurrentSource="user", other="x"))
        self.assertEqual(delivery.capture(context), COMPLETE)

    def test_a_partial_location_is_not_captured(self) -> None:
        """Carvana discards a partial location, so saving one would create a silent no-op.

        Verified live: zip alone, and zip+source+radius, both fell back to IP geolocation.
        """
        for partial in ({"CVCurrentZip": "89002"},
                        {"CVCurrentZip": "89002", "CVCurrentCity": "Henderson"},
                        {"CVCurrentCity": "Henderson", "CVCurrentState": "NV"}):
            context = _FakeContext(cookie_list(**partial))
            self.assertIsNone(delivery.capture(context), f"accepted partial {partial}")

    def test_blank_values_do_not_count_as_present(self) -> None:
        context = _FakeContext(cookie_list(CVCurrentZip="89002", CVCurrentCity="",
                                          CVCurrentState="NV"))
        self.assertIsNone(delivery.capture(context))

    def test_a_closed_context_yields_nothing_rather_than_raising(self) -> None:
        self.assertIsNone(delivery.capture(_FakeContext(raise_on_cookies=True)))


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        handle, path = tempfile.mkstemp(prefix="delivery-", suffix=".json")
        Path(path).unlink()
        self.path = Path(path)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def test_save_then_load(self) -> None:
        delivery.save(COMPLETE, self.path)
        self.assertEqual(delivery.load(self.path), COMPLETE)

    def test_missing_file_loads_as_none(self) -> None:
        self.assertIsNone(delivery.load(self.path))

    def test_corrupt_file_degrades_to_none(self) -> None:
        """A bad file must not break a run — it should just price against the IP and warn."""
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(delivery.load(self.path))

    def test_partial_saved_file_loads_as_none(self) -> None:
        self.path.write_text(json.dumps({"CVCurrentZip": "89002"}), encoding="utf-8")
        self.assertIsNone(delivery.load(self.path))

    def test_non_object_file_loads_as_none(self) -> None:
        self.path.write_text(json.dumps(["89002"]), encoding="utf-8")
        self.assertIsNone(delivery.load(self.path))

    def test_save_creates_the_parent_directory(self) -> None:
        nested = self.path.parent / "delivery-nested-test" / "loc.json"
        self.addCleanup(lambda: (nested.unlink(missing_ok=True),
                                 nested.parent.rmdir() if nested.parent.exists() else None))
        delivery.save(COMPLETE, nested)
        self.assertEqual(delivery.load(nested), COMPLETE)


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        handle, path = tempfile.mkstemp(prefix="delivery-", suffix=".json")
        Path(path).unlink()
        self.path = Path(path)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def test_apply_sets_all_three_cookies_on_the_carvana_domain(self) -> None:
        delivery.save(COMPLETE, self.path)
        context = _FakeContext()
        applied = delivery.apply(context, self.path)
        self.assertEqual(applied, COMPLETE)
        by_name = {c["name"]: c for c in context.added}
        self.assertEqual(set(by_name), set(delivery.REQUIRED_COOKIES))
        for cookie in context.added:
            self.assertEqual(cookie["domain"], ".carvana.com")
            self.assertEqual(cookie["path"], "/")
            self.assertTrue(cookie["secure"])

    def test_apply_with_nothing_saved_is_a_no_op(self) -> None:
        context = _FakeContext()
        self.assertIsNone(delivery.apply(context, self.path))
        self.assertEqual(context.added, [])

    def test_apply_survives_a_context_that_rejects_cookies(self) -> None:
        delivery.save(COMPLETE, self.path)
        self.assertIsNone(delivery.apply(_FakeContext(raise_on_add=True), self.path))


class DescribeTests(unittest.TestCase):
    def test_describes_a_saved_location(self) -> None:
        self.assertEqual(delivery.describe(COMPLETE), "89002 (Henderson, NV)")

    def test_describes_the_absence_of_one(self) -> None:
        self.assertIn("no saved delivery location", delivery.describe(None))


class SessionIntegrationTests(unittest.TestCase):
    """The replay must happen before any navigation, or it cannot affect pricing."""

    def test_session_restores_the_location_before_yielding(self) -> None:
        from carvana_scraper import browser

        seen: list[str] = []
        saved_apply = delivery.apply
        delivery.apply = lambda context, *a, **k: (seen.append("applied"), COMPLETE)[1]
        try:
            # Stand in for the Playwright launch, recording ordering only.
            class FakePw:
                class chromium:
                    @staticmethod
                    def launch_persistent_context(*a, **k):
                        return _FakeContext()

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            saved_pw = browser.sync_playwright
            browser.sync_playwright = lambda: FakePw()
            try:
                with tempfile.TemporaryDirectory() as profile:
                    with browser.session(profile_dir=profile) as context:
                        seen.append("yielded")
            finally:
                browser.sync_playwright = saved_pw
        finally:
            delivery.apply = saved_apply

        self.assertEqual(seen, ["applied", "yielded"],
                         "the location must be applied before the caller can navigate")

    def test_login_does_not_restore_the_location(self) -> None:
        """The operator is about to choose it; pre-seeding would show a location they didn't pick."""
        import inspect

        from carvana_scraper import browser

        source = inspect.getsource(browser.login)
        self.assertIn("restore_location=False", source)


if __name__ == "__main__":
    unittest.main()
