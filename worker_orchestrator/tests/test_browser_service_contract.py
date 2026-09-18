from pathlib import Path
import os
import re
import subprocess
import tempfile
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
        self.assertNotIn("PrivateTmp=true", unit)


    def test_chrome_launcher_has_bounded_display_readiness_gate(self):
        launcher = self.text("scripts/run_browser_wake_chrome.sh")
        self.assertIn("BROWSER_DISPLAY_READY_TIMEOUT_SECONDS", launcher)
        self.assertIn("BLOCKED_DISPLAY_NOT_READY", launcher)
        self.assertIn("DISPLAY_READY", launcher)
        self.assertIn("xset -display", launcher)
        self.assertLess(launcher.index("while ! display_ready"), launcher.index('exec "$chrome_bin"'))

    def test_display_readiness_simulation_waits_then_succeeds_without_browser(self):
        launcher = ROOT / "scripts" / "run_browser_wake_chrome.sh"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fakebin = root / "bin"
            fakebin.mkdir()
            counter = root / "counter"
            (fakebin / "ss").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (fakebin / "xset").write_text(
                "#!/bin/sh\n"
                f"n=$(cat {counter!s} 2>/dev/null || echo 0)\n"
                f"n=$((n+1)); echo $n > {counter!s}\n"
                "[ $n -ge 3 ]\n",
                encoding="utf-8",
            )
            (fakebin / "xdpyinfo").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            for file in fakebin.iterdir():
                file.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PATH": f"{fakebin}:/usr/bin:/bin",
                "CHATGPT_PROFILE_DIR": str(root / "profile"),
                "CHATGPT_CHROME_BIN": "/bin/true",
                "CHATGPT_BROWSER_URL": "http://127.0.0.1:9224",
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "DISPLAY": ":99",
                "BROWSER_DISPLAY_READY_TIMEOUT_SECONDS": "5",
                "BROWSER_DISPLAY_READY_POLL_SECONDS": "0.05",
                "BROWSER_WAKE_CHROME_READINESS_ONLY": "1",
            })
            (root / "runtime").mkdir()
            proc = subprocess.run(
                ["bash", str(launcher)], capture_output=True, text=True, env=env, timeout=10
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("DISPLAY_READY readiness_only=1", proc.stdout)
            self.assertGreaterEqual(int(counter.read_text().strip()), 3)

    def test_display_readiness_simulation_times_out_fail_closed(self):
        launcher = ROOT / "scripts" / "run_browser_wake_chrome.sh"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fakebin = root / "bin"
            fakebin.mkdir()
            for name, body in {
                "ss": "#!/bin/sh\nexit 1\n",
                "xset": "#!/bin/sh\nexit 1\n",
                "xdpyinfo": "#!/bin/sh\nexit 1\n",
            }.items():
                path = fakebin / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PATH": f"{fakebin}:/usr/bin:/bin",
                "CHATGPT_PROFILE_DIR": str(root / "profile"),
                "CHATGPT_CHROME_BIN": "/bin/true",
                "CHATGPT_BROWSER_URL": "http://127.0.0.1:9224",
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "DISPLAY": ":99",
                "BROWSER_DISPLAY_READY_TIMEOUT_SECONDS": "1",
                "BROWSER_DISPLAY_READY_POLL_SECONDS": "0.05",
                "BROWSER_WAKE_CHROME_READINESS_ONLY": "1",
            })
            (root / "runtime").mkdir()
            proc = subprocess.run(
                ["bash", str(launcher)], capture_output=True, text=True, env=env, timeout=5
            )
            self.assertEqual(proc.returncode, 75)
            self.assertIn("BLOCKED_DISPLAY_NOT_READY", proc.stderr)

    def test_dispatcher_depends_on_chrome_and_health(self):
        unit = self.text("systemd/browser-wake.service")
        self.assertIn("Requires=browser-wake-chrome.service", unit)
        self.assertIn("browser_chatgpt_health.mjs --wait=120", unit)
        self.assertIn("run_browser_wake_dispatcher.sh", unit)
        launcher = self.text("scripts/run_browser_wake_dispatcher.sh")
        self.assertIn("export GITHUB_TOKEN=", launcher)
        self.assertNotIn("export GH_TOKEN=", launcher)

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
