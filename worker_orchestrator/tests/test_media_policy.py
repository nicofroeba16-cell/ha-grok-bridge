from __future__ import annotations
import unittest
from worker_orchestrator.control_plane import MasterRequestError, parse_master_request
from worker_orchestrator.goals import parse_goal_text
from worker_orchestrator.media_policy import AUTO_MEDIA_POLICY_ID, requires_visual_media


def goal_text(extra=''):
    return '\n'.join([
        'GOAL PROMPT','PROJECT: Auto Chat','CHAT: AUTO - Example','GOAL_VERSION: v1',
        'REPOSITORY: nicofroeba16-cell/ha-grok-bridge','BRANCH: auto/test', extra,
        'DONE_CRITERIA:','- tests green',
    ])


def master_body(node_extra=''):
    extra = (',' + node_extra) if node_extra else ''
    return '\n'.join([
        'MASTER_REQUEST','REQUEST_ID: media-policy','GOAL_VERSION: v1','REQUEST: test',
        'WORK_GRAPH_JSON:',
        '[{"id":"child","project":"Auto Chat","chat":"AUTO - Example","repository":"nicofroeba16-cell/ha-grok-bridge","branch":"auto/test","done_criteria":["green"]' + extra + '}]',
        'GLOBAL_DONE_CRITERIA:','- green',
    ])


class MediaPolicyTests(unittest.TestCase):
    def test_omitted_scope_defaults_non_visual(self):
        goal = parse_goal_text(goal_text())
        self.assertIsNotNone(goal)
        self.assertFalse(goal.ui_visual_scope)
        self.assertFalse(requires_visual_media(goal))

    def test_simulation_ready_text_does_not_trigger_media(self):
        goal = parse_goal_text(goal_text('SCOPE: technical SIMULATION_READY acceptance'))
        self.assertFalse(requires_visual_media(goal))

    def test_explicit_visual_flags_trigger_only_when_true(self):
        self.assertFalse(requires_visual_media(parse_goal_text(goal_text('UI_VISUAL_SCOPE: false'))))
        self.assertTrue(requires_visual_media(parse_goal_text(goal_text('UI_VISUAL_SCOPE: true'))))
        self.assertTrue(requires_visual_media(parse_goal_text(goal_text('VISUAL_MEDIA_REQUIRED: true'))))

    def test_child_prompt_carries_explicit_false_by_default(self):
        req = parse_master_request(master_body(), 1)
        child = req.children[0]
        prompt = child.prompt(req.version)
        self.assertIn('UI_VISUAL_SCOPE: false', prompt)
        self.assertIn('VISUAL_MEDIA_REQUIRED: false', prompt)
        self.assertIn(f'AUTO_MEDIA_POLICY_ID: {AUTO_MEDIA_POLICY_ID}', prompt)

    def test_master_graph_boolean_false_string_is_false(self):
        req = parse_master_request(master_body('"ui_visual_scope":"false"'), 1)
        self.assertFalse(req.children[0].ui_visual_scope)

    def test_invalid_visual_flag_fails_closed(self):
        with self.assertRaises(MasterRequestError):
            parse_master_request(master_body('"ui_visual_scope":"sometimes"'), 1)


if __name__ == '__main__':
    unittest.main()
