from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "browser_chatgpt_send.mjs"


class BrowserSenderCompletionContractTests(unittest.TestCase):
    def test_completion_does_not_require_active_send_button(self):
        source = HELPER.read_text()
        completion = source.split("// Do not reload while ChatGPT is still producing the response.", 1)[1]
        completion = completion.split("// A newly-rendered user turn can be optimistic UI only.", 1)[0]
        self.assertNotIn("sendReady", completion)
        self.assertNotIn("const sendButton", completion)
        self.assertIn("waitForAssistantCompletion", completion)
        source = HELPER.read_text()
        self.assertIn("async function waitForPersistedUserTurn", source)
        self.assertIn("timeoutMs = 60000", source)
        self.assertIn("transient ChatGPT redirects/navigation context changes", source)
        self.assertIn("data-message-id", source)
        self.assertIn("hasNewIdentity", source)
        self.assertIn("identityAvailable ? hasNewIdentity : assistantTurns.length > before.count", source)
        self.assertIn("sameConversation", source)
        self.assertIn("destination_verified: true", source)
        self.assertIn("persisted_user_turn_after_reload_same_conversation", source)


if __name__ == "__main__":
    unittest.main()
