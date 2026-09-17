import unittest
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class VisualContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_mobile_layout_is_dense_and_body_safe(self):
        self.assertIn('name="viewport"', self.html)
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("@media(max-width:650px)", self.html)
        self.assertIn(".workers{grid-template-columns:1fr}", self.html)
        self.assertIn(".signal-strip{display:flex;overflow-x:auto", self.html)
        self.assertIn(".graph-scroll{max-width:100%;overflow-x:auto", self.html)

    def test_desktop_uses_three_worker_columns_and_non_stretched_hero(self):
        self.assertIn(".hero{grid-template-columns:minmax(0,1.6fr) minmax(320px,.9fr);align-items:start}", self.html)
        self.assertIn(".workers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))", self.html)
        self.assertIn("@media(max-width:1120px)", self.html)

    def test_operator_navigation_and_hierarchy_are_explicit(self):
        for anchor in ("#overview", "#workers-section", "#evidence-section", "#activity-section", "#system-section"):
            self.assertIn(f'href="{anchor}"', self.html)
        master = self.html.index('id="master"')
        attention = self.html.index('id="attention"')
        workers = self.html.index('id="workers"')
        evidence = self.html.index('id="evidence"')
        self.assertLess(master, attention)
        self.assertLess(attention, workers)
        self.assertLess(workers, evidence)

    def test_attention_triage_prioritizes_actionable_states_and_system_signals(self):
        self.assertIn("function attentionPriority(w)", self.html)
        self.assertIn("s==='WAITING_FOR_USER'?0:s==='ERROR'?1:s==='BLOCKED'?2", self.html)
        self.assertIn("Source degraded", self.html)
        self.assertIn("Wake UNCERTAIN", self.html)
        self.assertIn("stale Evidence", self.html)

    def test_all_operational_states_and_delivery_semantics_remain_distinct(self):
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

    def test_stale_evidence_is_timestamp_based_and_visible(self):
        self.assertIn("function ageHours(ts)", self.html)
        self.assertIn("age!==null&&age>24", self.html)
        self.assertIn("STALE &gt;24H", self.html)
        self.assertIn("updated_at", self.html)

    def test_large_dataset_dom_caps_are_preserved(self):
        self.assertIn("const LIMITS={workers:80,evidence:80,events:80,wakes:40,routes:80,attention:6,graph:18}", self.html)
        self.assertIn("function bounded(value,limit)", self.html)
        self.assertIn("DOM bewusst begrenzt", self.html)
        self.assertIn("Graph zeigt", self.html)

    def test_live_refresh_is_logically_idempotent_and_degraded_is_truthful(self):
        self.assertIn("let lastSignature=''", self.html)
        self.assertIn("function payloadSignature(d)", self.html)
        self.assertIn("key==='generated_at'?undefined:value", self.html)
        self.assertIn("if(signature===lastSignature)", self.html)
        self.assertIn("DEGRADED · Read-only", self.html)
        self.assertIn("Reconnecting · letzte Ansicht", self.html)
        self.assertIn("sourceIsHealthy", self.html)

    def test_progressive_disclosure_keeps_evidence_available(self):
        self.assertIn("<details>", self.html)
        self.assertIn("Evidence details", self.html)
        self.assertIn("Worker report · unverified", self.html)
        self.assertIn("Shared target:", self.html)

    def test_accessibility_and_reduced_motion_contract(self):
        self.assertIn(":focus-visible", self.html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", self.html)
        self.assertIn('aria-label="Dashboard Bereiche"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('role="img"', self.html)
        self.assertIn('aria-label="Master goal dependency graph"', self.html)

    def test_read_only_contract_remains_visible_and_no_mutation_fetch_exists(self):
        self.assertIn("READ-ONLY v4", self.html)
        for verb in ("retry", "restart", "deploy", "merge", "approve", "delete", "wake"):
            self.assertNotIn(f"fetch('/api/{verb}", self.html)


if __name__ == "__main__":
    unittest.main()
