from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class GitHubError(RuntimeError):
    pass


@dataclass(slots=True)
class GitHubClient:
    token: str
    api_base: str = "https://api.github.com"

    @classmethod
    def from_env(cls) -> "GitHubClient":
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise GitHubError("GITHUB_TOKEN is required; token value is never logged")
        return cls(token=token)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api_base + path,
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "runner-worker-orchestrator/0.1",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode(errors="replace")[:500]
            raise GitHubError(f"GitHub API {exc.code}: {msg}") from None

    def read_issue_items(self, repo: str, issue: int) -> list[dict]:
        issue_obj = self._request("GET", f"/repos/{repo}/issues/{issue}")
        comments: list[dict] = []
        page = 1
        while True:
            batch = self._request("GET", f"/repos/{repo}/issues/{issue}/comments?per_page=100&page={page}")
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return [{"id": None, "body": issue_obj.get("body", "")}, *comments]

    def read_master_items(self, repo: str, issue: int) -> list[dict]:
        """Backward-compatible name for the canonical Master issue read."""
        return self.read_issue_items(repo, issue)

    def exact_head_ci(self, repo: str, head: str) -> str:
        if not head:
            return "UNKNOWN"
        status = self._request("GET", f"/repos/{repo}/commits/{head}/status")
        runs = self._request("GET", f"/repos/{repo}/actions/runs?head_sha={head}&per_page=100")
        workflow_runs = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
        if workflow_runs:
            if any(r.get("status") != "completed" for r in workflow_runs):
                return "PENDING"
            good = {"success", "neutral", "skipped"}
            if any((r.get("conclusion") or "").lower() not in good for r in workflow_runs):
                return "RED"
            return "GREEN"
        state = (status.get("state") or "").lower() if isinstance(status, dict) else ""
        return {"success": "GREEN", "pending": "PENDING", "failure": "RED", "error": "RED"}.get(state, "UNKNOWN")

    def post_issue_comment(self, repo: str, issue: int, body: str) -> None:
        self._request("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": body})
