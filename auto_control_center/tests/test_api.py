import unittest

from auto_control_center.app import app


class ApiSurfaceTests(unittest.TestCase):
    def test_api_is_read_only(self):
        forbidden = set()
        for route in app.routes:
            for method in getattr(route, "methods", set()):
                if method not in {"GET", "HEAD"}:
                    forbidden.add((route.path, method))
        self.assertEqual(forbidden, set())

    def test_required_read_only_routes_exist(self):
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
        }
        self.assertTrue(expected.issubset(paths))

    def test_no_mutation_named_endpoint_exists(self):
        paths = " ".join(route.path.lower() for route in app.routes)
        for verb in ("wake", "retry", "restart", "merge", "deploy", "approve", "delete", "rotate"):
            if verb == "wake":
                # /api/wakes is a read-only history endpoint and is explicitly part of v1.
                continue
            self.assertNotIn(f"/{verb}", paths)


if __name__ == "__main__":
    unittest.main()
