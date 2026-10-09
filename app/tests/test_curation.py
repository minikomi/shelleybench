import tempfile
import unittest
from pathlib import Path

from app import ui
from app.server import App
from app.store import Store


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = App.__new__(App)
        self.app.store = Store(Path(self.tmp.name) / "bench.sqlite3")
        for key, order in (("older", None), ("third", 3), ("first", 1), ("second", 2)):
            self.app.store.put("tasks", {"id": key, "title": key + " task"})
            self.app.store.put("benchmarks", {
                "id": key, "title": key + " study", "featured_order": order,
                "group": "Studies", "status": "complete", "task_ids": [key],
                "models": [key + " model"], "versions": [], "attempts": 1,
            })

    def test_home_shows_only_featured_studies_in_order(self):
        title, html, live = ui.benchmarks_page(self.app, {})
        self.assertEqual(title, "Studies")
        self.assertLess(html.index("first study"), html.index("second study"))
        self.assertLess(html.index("second study"), html.index("third study"))
        self.assertNotIn("older study", html)
        self.assertNotIn("older model", html)
        self.assertNotIn("older task", html)
        self.assertIn('href="/?view=all">Archive', html)
        self.assertIn("Filter studies", html)
        self.assertFalse(live)

    def test_archive_preserves_every_study_and_filter_mode(self):
        title, html, _ = ui.benchmarks_page(self.app, {"view": ["all"]})
        self.assertEqual(title, "Archive")
        self.assertIn("older study", html)
        self.assertIn('name="view" value="all"', html)
        self.assertIn('href="/?view=all">Clear', html)
        self.assertIn('href="/">Curated studies', html)

    def test_filtering_curated_studies_does_not_search_the_archive(self):
        _, html, _ = ui.benchmarks_page(self.app, {"q": ["older"]})
        self.assertNotIn('href="/benchmarks/older"', html)
        self.assertIn("No benchmarks match", html)

    def test_unconfigured_site_keeps_all_benchmarks_visible(self):
        for key in ("first", "second", "third"):
            self.app.store.update("benchmarks", key, featured_order=None)
        title, html, _ = ui.benchmarks_page(self.app, {})
        self.assertEqual(title, "Benchmarks")
        self.assertIn("older study", html)
        self.assertNotIn("Filter studies", html)

    def test_metadata_api_validates_order_and_allows_unfeaturing(self):
        for value in (-1, 1001, True, "1"):
            with self.assertRaisesRegex(ValueError, "featured_order"):
                self.app.update_benchmark("older", {"featured_order": value})
        self.app.update_benchmark("older", {"featured_order": 0})
        _, html, _ = ui.benchmarks_page(self.app, {})
        self.assertLess(html.index("older study"), html.index("first study"))
        self.app.update_benchmark("older", {"featured_order": None})
        _, html, _ = ui.benchmarks_page(self.app, {})
        self.assertNotIn("older study", html)
