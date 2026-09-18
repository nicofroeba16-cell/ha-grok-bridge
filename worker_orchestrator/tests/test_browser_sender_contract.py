from pathlib import Path
import json
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "browser_chatgpt_send.mjs"
ATOMIC_FIXTURE = ROOT / "tests" / "browser_atomic_composer_fixture.mjs"


class BrowserSenderCompletionContractTests(unittest.TestCase):
    def test_completion_does_not_require_active_send_button(self):
        source = HELPER.read_text()
        completion = source.split("// Do not reload while ChatGPT is still producing the response.", 1)[1]
        completion = completion.split("// A newly-rendered user turn can be optimistic UI only.", 1)[0]
        self.assertNotIn("sendReady", completion)
        self.assertNotIn("const sendButton", completion)
        self.assertIn("waitForAssistantCompletion", completion)
        self.assertIn("async function waitForPersistedUserTurn", source)
        self.assertIn("timeoutMs = 60000", source)
        self.assertIn("transient ChatGPT redirects/navigation context changes", source)
        self.assertIn("data-message-id", source)
        self.assertIn("hasNewIdentity", source)
        self.assertIn("identityAvailable ? hasNewIdentity : assistantTurns.length > before.count", source)
        self.assertIn("sameConversation", source)
        self.assertIn("destination_verified: true", source)
        self.assertIn("persisted_user_turn_after_reload_same_conversation", source)
        self.assertIn("target_idle_verified_before_composer_mutation: true", source)
        self.assertIn("atomic_payload_readback_verified: true", source)
        self.assertIn("exactly_one_full_wake_turn_verified: true", source)

    def test_pre_send_idle_gate_precedes_every_composer_mutation(self):
        source = HELPER.read_text()
        idle = source.index("const idle = await waitForStableConversationIdle(page)")
        atomic = source.index("await setComposerTextAtomically(page, composer, request.payload)")
        committed = source.index("sendCommitted = true")
        click = source.index("await send.click()")
        self.assertLess(idle, atomic)
        self.assertLess(atomic, committed)
        self.assertLess(committed, click)
        self.assertIn("target conversation remained busy before wake insertion", source)
        self.assertIn("stop_control_visible", source)
        self.assertIn("positive_generation_indicator", source)
        self.assertNotIn("await page.keyboard.type(request.payload)", source)
        self.assertNotIn("page.keyboard.press('Enter')", source)
        self.assertNotIn("stopControl.click", source)

    def test_atomic_multiline_contract_verifies_full_payload_before_single_send(self):
        source = HELPER.read_text()
        atomic = source.index("await setComposerTextAtomically(page, composer, request.payload)")
        readback = source.index("atomic composer payload readback mismatch before send")
        preturn = source.index("wake user turn appeared before explicit Send")
        click = source.index("await send.click()")
        post = source.index("waitForExactlyOneWakeTurn(", click)
        self.assertLess(atomic, readback)
        self.assertLess(readback, preturn)
        self.assertLess(preturn, click)
        self.assertLess(click, post)
        self.assertEqual(source.count("await send.click();"), 1)
        self.assertIn("exactPrefixOnly", source)
        self.assertIn("persisted exactly once without a prefix-only wake turn", source)

    def test_atomic_composer_fixture_models_enter_to_send_regression(self):
        proc = subprocess.run(
            ["node", str(ATOMIC_FIXTURE)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.strip())
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["cases"],
            [
                "master-atomic",
                "worker-atomic",
                "busy-zero-mutation",
                "stable-idle-delivery",
            ],
        )


if __name__ == "__main__":
    unittest.main()
