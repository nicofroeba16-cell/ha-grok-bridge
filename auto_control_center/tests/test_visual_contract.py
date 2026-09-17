import unittest
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class VisualContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_mobile_breakpoint_collapses_worker_grid(self):
        self.assertIn('name="viewport"', self.html)
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("@media(max-width:650px)", self.html)
        self.assertIn(".workers{grid-template-columns:1fr}", self.html)
        self.assertIn(".cards{grid-template-columns:repeat(2,1fr)}", self.html)

    def test_desktop_summary_includes_registry_drift(self):
        self.assertIn('id="statRegistry"', self.html)
        self.assertIn("registry_shared_target", self.html)
        self.assertIn("keine kanonische Identität abgeleitet", self.html)

    def test_uncertain_wake_has_distinct_visual_semantics(self):
        self.assertIn(".wake-uncertain{color:var(--warn)}", self.html)
        self.assertIn("weder Erfolg noch Fehler", self.html)
        self.assertIn("status_class", self.html)

    def test_shared_target_and_wake_copy_remain_read_only(self):
        self.assertIn("READ-ONLY v2", self.html)
        self.assertNotIn("fetch('/api/retry", self.html)
        self.assertNotIn("fetch('/api/restart", self.html)
        self.assertNotIn("fetch('/api/deploy", self.html)


if __name__ == "__main__":
    unittest.main()
