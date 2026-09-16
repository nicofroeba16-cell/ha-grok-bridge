import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bridge", ROOT / "ha_grok_bridge" / "file_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ExclusionPolicyTests(unittest.TestCase):
    def test_runtime_recovery_directories_are_excluded(self):
        for name in (
            ".firetv-companion-backups",
            ".patch-backups",
            ".patch-stage",
            ".patch-staging",
            "luna-backups",
            "ha-grok-bridge",
            "ha-intelligence-suite",
        ):
            with self.subTest(name=name):
                self.assertTrue(bridge.excluded(name, {}))

    def test_transient_metadata_is_excluded(self):
        for name in (".ha_run.lock", ".hass_configurator_prefs.json", ".ssh_known_hosts"):
            with self.subTest(name=name):
                self.assertTrue(bridge.excluded(name, {}))

    def test_backup_globs_are_precise(self):
        for name in (
            "apple.yaml.bak",
            "apple.yaml.bak-20260827",
            "settings.backup",
            "settings.backup-20260916",
            "automations.yaml.firetv-realtime-backup",
        ):
            with self.subTest(name=name):
                self.assertTrue(bridge.excluded(name, {}))
        self.assertFalse(bridge.excluded("backup.py", {}))
        self.assertFalse(bridge.excluded("configuration.yaml", {}))

    def test_nested_paths_follow_the_policy(self):
        self.assertTrue(bridge.ignored(Path(".patch-backups/run/file.txt"), {}))
        self.assertTrue(bridge.ignored(Path("www/apple-optik.js.bak-20260827"), {}))
        self.assertFalse(bridge.ignored(Path("custom_components/hacs/utils/backup.py"), {}))
        self.assertFalse(bridge.ignored(Path("www/apple-gradient-ab-test.js"), {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
