from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Mapping

from .browser_wake import (
    BrowserRoute,
    BrowserRouteRegistry,
    BrowserWakeError,
    CommandBrowserSender,
    WakeCoordinator,
    build_parser,
    format_verified_delivery_event,
)


class SearchRouteRegistry(BrowserRouteRegistry):
    """Route registry that accepts either a concrete ChatGPT URL or an exact chat title."""

    @staticmethod
    def _validate_title(raw: str) -> str:
        value = " ".join(raw.split()).strip()
        if not value:
            raise BrowserWakeError("browser route title must not be empty")
        if len(value) > 200:
            raise BrowserWakeError("browser route title exceeds 200 characters")
        if "\n" in raw or "\r" in raw:
            raise BrowserWakeError("browser route title must be a single line")
        return value

    @classmethod
    def from_json(cls, raw: str) -> "SearchRouteRegistry":
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                key = str(key)
                if key in result:
                    raise BrowserWakeError(f"browser route registry contains duplicate key: {key}")
                result[key] = value
            return result

        try:
            value = json.loads(raw, object_pairs_hook=unique_pairs)
        except json.JSONDecodeError as exc:
            raise BrowserWakeError(f"invalid BROWSER_CHAT_ROUTES_JSON: {exc.msg}") from exc
        if not isinstance(value, Mapping):
            raise BrowserWakeError("BROWSER_CHAT_ROUTES_JSON must be an object")
        routes: dict[str, BrowserRoute] = {}
        destinations: dict[str, str] = {}
        for key, config in value.items():
            if not isinstance(config, Mapping):
                raise BrowserWakeError(f"browser route {key} must be an object")
            has_url = bool(str(config.get("url", "")).strip())
            has_title = bool(str(config.get("title", "")).strip())
            if has_url == has_title:
                raise BrowserWakeError(
                    f"browser route {key} must define exactly one of url or title"
                )
            if has_url:
                destination = cls._validate_url(str(config.get("url", "")))
            else:
                title = cls._validate_title(str(config.get("title", "")))
                destination = f"chat-title:{title}"
            route_key = str(key)
            if destination in destinations and destinations[destination] != route_key:
                raise BrowserWakeError(
                    f"browser route destination is ambiguous for {route_key} and {destinations[destination]}"
                )
            destinations[destination] = route_key
            routes[route_key] = BrowserRoute(route_key, destination)
        return cls(routes)

    @classmethod
    def from_file(cls, path: str | Path) -> "SearchRouteRegistry":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.routes_file:
        raise SystemExit("BROWSER_CHAT_ROUTES_FILE is required")
    routes = SearchRouteRegistry.from_file(args.routes_file)
    sender = CommandBrowserSender(args.browser_command)

    from .github_client import GitHubClient

    gh = GitHubClient.from_env()
    connection = sqlite3.connect(args.db)
    coordinator = WakeCoordinator(
        connection,
        routes,
        sender,
        master_repo=args.master_repo,
        master_issue=args.master_issue,
        debounce_seconds=args.debounce_seconds,
        verified_delivery_reporter=lambda record: gh.post_issue_comment(
            args.master_repo,
            args.master_issue,
            format_verified_delivery_event(record),
        ),
    )

    def run_once() -> dict[str, int | str]:
        return coordinator.reconcile(
            gh.read_master_items(args.master_repo, args.master_issue),
            replay_existing=args.replay_existing,
        )

    try:
        if args.cmd == "once":
            print(json.dumps(run_once(), sort_keys=True))
            return 0
        while True:
            try:
                print(json.dumps(run_once(), sort_keys=True), flush=True)
            except Exception as exc:
                print(
                    json.dumps(
                        {"state": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:240]}
                    ),
                    flush=True,
                )
            time.sleep(max(5, args.poll_seconds))
    except KeyboardInterrupt:
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
