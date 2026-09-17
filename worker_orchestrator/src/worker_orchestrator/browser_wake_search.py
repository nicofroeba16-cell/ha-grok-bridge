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
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise BrowserWakeError("BROWSER_CHAT_ROUTES_JSON must be an object")
        routes: dict[str, BrowserRoute] = {}
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
            routes[str(key)] = BrowserRoute(str(key), destination)
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
    connection = sqlite3.connect(args.db)
    coordinator = WakeCoordinator(
        connection,
        routes,
        sender,
        master_repo=args.master_repo,
        master_issue=args.master_issue,
        debounce_seconds=args.debounce_seconds,
    )

    from .github_client import GitHubClient

    gh = GitHubClient.from_env()

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
