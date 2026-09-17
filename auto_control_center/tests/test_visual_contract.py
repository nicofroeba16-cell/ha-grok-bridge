import unittest
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class VisualContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_mobile_breakpoint_collapses_worker_grid_without_body_hacks(self):
        self.assertIn('name="viewport"', self.html)
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("@media(max-width:650px)", self.html)
        self.assertIn(".workers{grid-template-columns:1fr}", self.html)
        self.assertIn(".cards{grid-template-columns:repeat(2,minmax(0,1fr))}", self.html)
        self.assertIn(".graph-scroll{max-width:100%;overflow-x:auto", self.html)

    def test_all_operational_states_have_dedicated_semantics(self):
        for token in (
            ".state-DONE",
            ".state-RUNNING",
            ".state-READY",
            ".state-BLOCKED",
            ".state-WAITING_FOR_USER",
            ".state-DORMANT",
            ".state-ERROR",
        ):
            self.assertIn(token, self.html)
        self.assertIn(".wake-uncertain", self.html)
        self.assertIn("nicht verifiziert; kein Erfolg und kein Fehler", self.html)

    def test_information_hierarchy_prioritizes_master_and_attention(self):
        master = self.html.index('id="master"')
        attention = self.html.index('id="attention"')
        workers = self.html.index('id="workers"')
        evidence = self.html.index('id="evidence"')
        self.assertLess(master, attention)
        self.assertLess(attention, workers)
        self.assertLess(workers, evidence)
        self.assertIn("Worker report · unverified", self.html)
        self.assertIn("<details>", self.html)

    def test_large_dataset_dom_is_explicitly_bounded(self):
        self.assertIn("const LIMITS={workers:80,evidence:80,events:80,wakes:40,routes:80,attention:6,graph:18}", self.html)
        self.assertIn("function bounded(value,limit)", self.html)
        self.assertIn("DOM bewusst begrenzt", self.html)
        self.assertIn("Graph zeigt", self.html)

    def test_live_refresh_is_idempotent_and_degraded_is_truthful(self):
        self.assertIn("let lastSignature=''", self.html)
        self.assertIn("if(signature===lastSignature)", self.html)
        self.assertIn("DEGRADED · Read-only", self.html)
        self.assertIn("Reconnecting · letzte Ansicht", self.html)
        self.assertIn("sourceIsHealthy", self.html)

    def test_accessibility_and_reduced_motion_contract(self):
        self.assertIn(":focus-visible", self.html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", self.html)
        self.assertIn('role="img"', self.html)
        self.assertIn('aria-label="Master goal dependency graph"', self.html)

    def test_read_only_contract_remains_visible_and_no_mutation_fetch_exists(self):
        self.assertIn("READ-ONLY v3", self.html)
        for verb in ("retry", "restart", "deploy", "merge", "approve", "delete"):
            self.assertNotIn(f"fetch('/api/{verb}", self.html)


if __name__ == "__main__":
    unittest.main()
