from __future__ import annotations

import json
import unittest

from worker_orchestrator.browser_wake import BrowserWakeError
from worker_orchestrator.browser_wake_search import SearchRouteRegistry


class SearchRouteRegistryTests(unittest.TestCase):
    def test_exact_title_route_is_encoded_without_guessing_url(self):
        registry = SearchRouteRegistry.from_json(json.dumps({
            "__master__": {"title": "Master-Verteilung"},
            "Projekt: IOS App → Chat: Run 24 Watch": {"title": "Run 24 Watch"},
        }))
        self.assertEqual(registry.get("__master__").url, "chat-title:Master-Verteilung")
        self.assertEqual(
            registry.get("Projekt: IOS App → Chat: Run 24 Watch").url,
            "chat-title:Run 24 Watch",
        )

    def test_concrete_url_still_supported(self):
        registry = SearchRouteRegistry.from_json(json.dumps({
            "worker": {"url": "https://chatgpt.com/c/12345678-abcd-1234-abcd-123456789abc"}
        }))
        self.assertTrue(registry.get("worker").url.startswith("https://chatgpt.com/c/"))

    def test_route_requires_exactly_one_locator(self):
        with self.assertRaises(BrowserWakeError):
            SearchRouteRegistry.from_json(json.dumps({"worker": {}}))
        with self.assertRaises(BrowserWakeError):
            SearchRouteRegistry.from_json(json.dumps({
                "worker": {
                    "title": "Run 24 Watch",
                    "url": "https://chatgpt.com/c/12345678-abcd-1234-abcd-123456789abc",
                }
            }))

    def test_multiline_title_fails_closed(self):
        with self.assertRaises(BrowserWakeError):
            SearchRouteRegistry.from_json(json.dumps({"worker": {"title": "Run 24\nWatch"}}))


if __name__ == "__main__":
    unittest.main()
