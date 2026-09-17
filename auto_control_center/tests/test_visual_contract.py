import unittest
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class VisualContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_mobile_navigation_is_segmented_and_not_scroll_dependent(self):
        self.assertIn('data-section="overview"', self.html)
        self.assertIn('data-section="system-section"', self.html)
        self.assertIn(".section-nav{display:grid;grid-template-columns:repeat(5,minmax(0,1fr))", self.html)
        self.assertIn(".section-nav a.active", self.html)
        self.assertIn("new IntersectionObserver", self.html)

    def test_mobile_signal_area_is_deliberate_compact_grid(self):
        self.assertIn(".signal-strip{grid-template-columns:repeat(3,minmax(0,1fr))", self.html)
        self.assertNotIn(".signal-strip{display:flex;overflow-x:auto", self.html)
        self.assertIn(".workers{grid-template-columns:1fr}", self.html)

    def test_desktop_uses_operator_composition_and_three_worker_columns(self):
        self.assertIn(".hero{grid-template-columns:minmax(0,1.62fr) minmax(330px,.9fr);align-items:start}", self.html)
        self.assertIn(".workers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))", self.html)
        self.assertIn("@media(max-width:1120px)", self.html)

    def test_human_readable_status_labels_are_mapped_at_presentation_layer(self):
        self.assertIn("WAITING_FOR_USER:'Action required'", self.html)
        self.assertIn("CANCELLED_SUPERSEDED:'Cancelled / superseded'", self.html)
        self.assertIn("FAILED_PRE_SEND:'Failed pre-send'", self.html)
        self.assertIn("const stateLabel=", self.html)
        self.assertIn("const wakeLabel=", self.html)

    def test_attention_triage_prioritizes_action_then_error_then_blocked(self):
        self.assertIn("s==='WAITING_FOR_USER'?0:s==='ERROR'?1:s==='BLOCKED'?2:s==='STALLED'?3", self.html)
        self.assertIn("Source degraded", self.html)
        self.assertIn("Wake delivery uncertain", self.html)
        self.assertIn("Stale evidence", self.html)
        self.assertIn("reserve=Math.min(2,system.length)", self.html)
        self.assertIn("attention-item ${tone} ${i===0?'primary':''}", self.html)

    def test_worker_search_filters_and_only_problems_are_read_only_client_controls(self):
        self.assertIn('id="workerSearch" type="search"', self.html)
        for mode in ("all", "action", "running", "error", "blocked", "complete"):
            self.assertIn(f'data-filter="{mode}"', self.html)
        self.assertIn('id="onlyProblems" type="checkbox"', self.html)
        self.assertIn("function renderWorkers(d)", self.html)
        self.assertIn("function isProblem(w)", self.html)

    def test_worker_cards_keep_actions_visible_and_evidence_progressively_disclosed(self):
        self.assertIn("function workerAction(w)", self.html)
        self.assertIn("<details><summary>Evidence details</summary>", self.html)
        self.assertIn("Worker report · unverified", self.html)
        self.assertIn("Shared target:", self.html)
        self.assertIn("Stale evidence:", self.html)

    def test_progress_is_not_repeated_across_multiple_master_fact_tiles(self):
        self.assertIn('class="master-status"', self.html)
        self.assertNotIn("master-facts", self.html)
        self.assertNotIn("master-fact", self.html)
        self.assertIn("relativeTime(m.updated_at)", self.html)

    def test_global_degraded_and_reconnecting_state_is_explicit(self):
        self.assertIn('id="globalState"', self.html)
        self.assertIn("[hidden]{display:none!important}", self.html)
        self.assertIn("Source degraded.", self.html)
        self.assertIn("Reconnecting.", self.html)
        self.assertIn("Offline.", self.html)
        self.assertIn("sourceIsHealthy", self.html)

    def test_evidence_summary_includes_freshness_ci_and_head(self):
        self.assertIn("fresh=relativeTime(w.updated_at)", self.html)
        self.assertIn("HEAD ${short(e.head)}", self.html)
        self.assertIn("CI ${esc(stateLabel(e.ci_status||'Unknown'))}", self.html)

    def test_large_dataset_dom_caps_and_idempotence_are_preserved(self):
        self.assertIn("const LIMITS={workers:80,evidence:80,events:80,wakes:40,routes:80,attention:6,graph:18}", self.html)
        self.assertIn("function bounded(value,limit)", self.html)
        self.assertIn("DOM bewusst begrenzt", self.html)
        self.assertIn("let lastSignature='',lastData=null", self.html)
        self.assertIn("if(signature===lastSignature)", self.html)

    def test_accessibility_and_reduced_motion_contract(self):
        self.assertIn(":focus-visible", self.html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", self.html)
        self.assertIn('aria-label="Control Center Bereiche"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('role="img"', self.html)
        self.assertIn('aria-label="Master goal dependency graph"', self.html)

    def test_read_only_contract_remains_visible_and_no_mutation_fetch_exists(self):
        self.assertIn("READ-ONLY v5", self.html)
        for verb in ("retry", "restart", "deploy", "merge", "approve", "delete", "wake"):
            self.assertNotIn(f"fetch('/api/{verb}", self.html)


if __name__ == "__main__":
    unittest.main()
