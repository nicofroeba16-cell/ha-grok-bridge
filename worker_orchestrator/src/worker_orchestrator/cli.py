from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import threading
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Orchestrator, format_report
from .github_client import GitHubClient
from .models import DEFAULT_ALLOWED_REPOSITORIES, Goal, LifecycleState
from .store import Registry
from .worker import CommandWorkerAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worker-orchestrator")
    parser.add_argument("--db", default=os.environ.get("ORCHESTRATOR_DB", "./worker-orchestrator.sqlite3"))
    parser.add_argument("--master-repo", default=os.environ.get("MASTER_REPO", "nicofroeba16-cell/ha-grok-bridge"))
    parser.add_argument("--master-issue", type=int, default=int(os.environ.get("MASTER_ISSUE", "3")))
    parser.add_argument("--worker-command", default=os.environ.get("WORKER_COMMAND", ""))
    parser.add_argument("--workspace-root", default=os.environ.get("WORKSPACE_ROOT", "/home/vboxuser/.local/share/worker-orchestrator/workspaces"))
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("RECONCILE_SECONDS", "300")))
    parser.add_argument(
        "--allow-non-dry-run",
        action="store_true",
        help="Allow ordinary non-live worker execution. Gated actions still need explicit goal approval.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once")
    run = sub.add_parser("run")
    run.add_argument("--webhook-host", default=os.environ.get("WEBHOOK_HOST", "127.0.0.1"))
    run.add_argument("--webhook-port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "8787")))
    sub.add_parser("status")
    return parser


def _goal_from_row(row) -> Goal:
    return Goal(
        project=row["project"],
        chat=row["chat"],
        repository=row["repository"],
        branch=row["branch"],
        prompt=row["prompt"],
        done_criteria=tuple(json.loads(row["done_criteria"])),
        workstream_issue=row["workstream_issue"],
        explicit_version=row["goal_version"],
        files=tuple(json.loads(row["files"])),
        scope=row["scope"],
        approved_actions=tuple(json.loads(row["approved_actions"])),
        source_comment_id=row["source_comment_id"],
    )


def make_reporter(gh: GitHubClient, master_repo: str, master_issue: int):
    def report(kind: str, goal: Goal, payload: dict) -> None:
        body = format_report(kind, goal, payload)
        target_issue = goal.workstream_issue or master_issue
        if target_issue != master_issue:
            gh.post_issue_comment(goal.repository, target_issue, body)
        gh.post_issue_comment(master_repo, master_issue, body)

    return report


def reconcile(engine: Orchestrator, gh: GitHubClient, master_repo: str, master_issue: int) -> None:
    try:
        engine.ingest_items(gh.read_master_items(master_repo, master_issue))
        rows = engine.registry.list_dispatchable()
    except Exception:
        # A failed poll must not take down the long-running daemon.
        return
    for row in rows:
        try:
            engine.dispatch_goal(_goal_from_row(row))
        except Exception as exc:
            # Isolate one broken assignment from the remaining workers.
            try:
                goal = _goal_from_row(row)
                engine.registry.set_state(
                    goal.key, LifecycleState.BLOCKED,
                    blockers=["ORCHESTRATOR_INTERNAL_ERROR"],
                    last_progress="ORCHESTRATOR_INTERNAL_ERROR",
                )
                engine.registry.record_event(
                    goal.key, goal.version, "ORCHESTRATOR_INTERNAL_ERROR", {"reason": str(exc)[:240]}
                )
            except Exception:
                pass


def webhook_server(host: str, port: int, wake: threading.Event, secret: str):
    if host not in {"127.0.0.1", "localhost", "::1"} and not secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is required for non-loopback webhook binding")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if secret:
                signature = self.headers.get("X-Hub-Signature-256", "")
                expected = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    self.send_response(401)
                    self.end_headers()
                    return
            event = self.headers.get("X-GitHub-Event", "")
            if event in {
                "issue_comment", "issues", "workflow_run", "push",
                "pull_request", "check_run", "check_suite",
            }:
                wake.set()
            self.send_response(202)
            self.end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = Registry(Path(args.db))

    if args.cmd == "status":
        for row in registry.list_all():
            safe = {
                key: row[key]
                for key in row.keys()
                if key not in {"prompt", "completion_evidence", "session_state"}
            }
            print(json.dumps(safe, sort_keys=True))
        registry.close()
        return 0

    registry.recover_interrupted()

    if not args.worker_command:
        raise SystemExit(
            "WORKER_COMMAND/--worker-command is required for execution; "
            "browser/UI automation is intentionally unsupported"
        )

    gh = GitHubClient.from_env()
    worker_env = tuple(
        x.strip()
        for x in os.environ.get("WORKER_ENV_ALLOWLIST", "").split(",")
        if x.strip()
    )
    allowed_repos_env = os.environ.get("ALLOWED_REPOSITORIES", "")
    allowed_repos = {
        x.strip() for x in allowed_repos_env.split(",") if x.strip()
    } or set(DEFAULT_ALLOWED_REPOSITORIES)

    engine = Orchestrator(
        registry,
        CommandWorkerAdapter(
            args.worker_command,
            env_allowlist=worker_env,
            workspace_root=args.workspace_root,
        ),
        reporter=make_reporter(gh, args.master_repo, args.master_issue),
        dry_run=not args.allow_non_dry_run,
        ci_verifier=gh.exact_head_ci,
        allowed_repositories=allowed_repos,
    )

    if args.cmd == "once":
        reconcile(engine, gh, args.master_repo, args.master_issue)
        registry.close()
        return 0

    wake = threading.Event()
    stop = threading.Event()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    server = webhook_server(args.webhook_host, args.webhook_port, wake, secret)

    def stop_handler(*_):
        stop.set()
        wake.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        while not stop.is_set():
            reconcile(engine, gh, args.master_repo, args.master_issue)
            wake.wait(timeout=max(30, args.poll_seconds))
            wake.clear()
    finally:
        server.shutdown()
        registry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
