import os
import unittest
from unittest.mock import patch

from auto_control_center.app import app
from auto_control_center.wake_all import capability_enabled


class ApiSurfaceTests(unittest.TestCase):
    def test_only_one_narrow_mutating_endpoint_exists(self):
        mutations = set()
        for route in app.routes:
            for method in getattr(route, "methods", set()):
                if method not in {"GET", "HEAD"}:
                    mutations.add((route.path, method))
        self.assertEqual(mutations, {("/api/actions/wake-all/submit", "POST")})

    def test_required_read_routes_and_guarded_action_routes_exist(self):
        paths = {route.path for route in app.routes}
        expected = {
            "/",
            "/api/health",
            "/api/dashboard",
            "/api/master",
            "/api/workers",
            "/api/events",
            "/api/wakes",
            "/api/routes",
            "/api/evidence",
            "/api/stream",
            "/api/actions/wake-all/preview",
            "/api/actions/wake-all/result",
            "/api/actions/wake-all/submit",
        }
        self.assertTrue(expected.issubset(paths))

    def test_no_generic_or_unrelated_mutation_endpoint_exists(self):
        paths = " ".join(route.path.lower() for route in app.routes)
        for verb in ("retry", "restart", "merge", "deploy", "approve", "delete", "rotate", "command", "exec"):
            self.assertNotIn("/" + verb, paths)

    def test_wake_capability_is_disabled_by_default(self):
        with patch.dict(os.environ, {"ACC_WAKE_ALL_ENABLED": ""}, clear=False):
            self.assertFalse(capability_enabled())


if __name__ == "__main__":
    unittest.main()
