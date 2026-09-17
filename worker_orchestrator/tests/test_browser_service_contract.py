from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BrowserServiceContractTests(unittest.TestCase):
    def text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_chrome_service_is_loopback_and_long_lived(self):
        unit = self.text("systemd/browser-wake-chrome.service")
        launcher = self.text("scripts/run_browser_wake_chrome.sh")
        self.assertIn("PartOf=master-autonomous-orchestration.target", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("--remote-debugging-address=127.0.0.1", launcher)
        self.assertIn("--remote-debugging-port=9224", launcher)
        self.assertIn("chrome-profile-web-v2", self.text("scripts/prepare_browser_wake_services.sh"))
        self.assertNotRegex(launcher, r"\b(?:pkill|killall)\b")
        self.assertNotIn("NoNewPrivileges=true", unit)

    def test_dispatcher_depends_on_chrome_and_health(self):
        unit = self.text("systemd/browser-wake.service")
        self.assertIn("Requires=browser-wake-chrome.service", unit)
        self.assertIn("browser_chatgpt_health.mjs --wait=120", unit)
        self.assertIn("run_browser_wake_dispatcher.sh", unit)

    def test_target_owns_both_services(self):
        target = self.text("systemd/master-autonomous-orchestration.target")
        self.assertIn("Requires=browser-wake-chrome.service browser-wake.service", target)
        self.assertIn("WantedBy=default.target", target)

    def test_prepare_never_activates_services(self):
        script = self.text("scripts/prepare_browser_wake_services.sh")
        self.assertIn("systemctl --user daemon-reload", script)
        self.assertIn("PREPARED_NOT_ACTIVATED", script)
        self.assertNotRegex(script, r"systemctl\s+--user\s+(?:start|restart|enable)\b")
        self.assertNotIn("--now", script)

    def test_health_probe_never_reads_assistant_content(self):
        helper = self.text("browser_chatgpt_health.mjs")
        self.assertIn("BLOCKED_CHALLENGE", helper)
        self.assertIn("BLOCKED_AUTH", helper)
        self.assertIn("HEALTHY", helper)
        self.assertNotIn("data-message-author-role", helper)
        self.assertNotIn("innerText", helper)
        self.assertNotIn("textContent", helper)
        self.assertRegex(helper, re.escape("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
