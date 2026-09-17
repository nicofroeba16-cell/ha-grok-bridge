from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .security import sanitize


class MasterRequestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChildGoal:
    child_id: str
    project: str
    chat: str
    repository: str
    branch: str
    done_criteria: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    workstream_issue: int | None = None
    files: tuple[str, ...] = ()
    scope: str = ""

    @property
    def worker_key(self) -> str:
        return f"Projekt: {self.project} → Chat: {self.chat}"

    def prompt(self, request_version: str) -> str:
        lines = [
            "GOAL PROMPT",
            f"PROJECT: {self.project}",
            f"CHAT: {self.chat}",
            f"GOAL_VERSION: {request_version}-{self.child_id}",
            f"REPOSITORY: {self.repository}",
            f"BRANCH: {self.branch}",
        ]
        if self.workstream_issue is not None:
            lines.append(f"WORKSTREAM_ISSUE: {self.workstream_issue}")
        if self.scope:
            lines.append(f"SCOPE: {self.scope}")
        if self.files:
            lines.append(f"FILES: {','.join(self.files)}")
        if self.dependencies:
            lines.append(f"DEPENDS_ON: {','.join(self.dependencies)}")
        lines.extend(["DONE_CRITERIA:", *[f"- {item}" for item in self.done_criteria]])
        lines.extend([
            "",
            "SAFETY:",
            "No merge, deploy, restart, runner/runtime mutation, device/network/HA mutation, "
            "secret/key change, destructive cleanup, release or tag mutation without an explicit "
            "approval in this exact goal.",
        ])
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class MasterRequest:
    request_id: str
    version: str
    request: str
    done_criteria: tuple[str, ...]
    children: tuple[ChildGoal, ...]
    source_comment_id: int
    content_hash: str


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def parse_master_request(text: str, source_comment_id: int | None = None) -> MasterRequest | None:
    if not text.lstrip().startswith("MASTER_REQUEST"):
        return None
    fields: dict[str, str] = {}
    graph_lines: list[str] = []
    in_graph = False
    done: list[str] = []
    in_done = False
    for raw in text.splitlines()[1:]:
        line = raw.strip()
        if line == "WORK_GRAPH_JSON:":
            in_graph, in_done = True, False
            continue
        if line in {"GLOBAL_DONE_CRITERIA:", "DONE_CRITERIA:"}:
            in_done, in_graph = True, False
            continue
        if in_graph:
            graph_lines.append(raw)
            continue
        if in_done and line.startswith("-"):
            done.append(line[1:].strip())
            continue
        if in_done and line and not line.startswith("-"):
            in_done = False
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().upper()] = value.strip()

    request_id = fields.get("REQUEST_ID", "")
    request_text = fields.get("REQUEST", "")
    if not request_id or not request_text or not done or not graph_lines:
        raise MasterRequestError("MASTER_REQUEST requires REQUEST_ID, REQUEST, GLOBAL_DONE_CRITERIA and WORK_GRAPH_JSON")
    try:
        raw_graph = json.loads("\n".join(graph_lines))
    except json.JSONDecodeError as exc:
        raise MasterRequestError(f"invalid WORK_GRAPH_JSON: {exc.msg}") from exc
    if not isinstance(raw_graph, list) or not raw_graph:
        raise MasterRequestError("WORK_GRAPH_JSON must be a non-empty array")
    children: list[ChildGoal] = []
    child_ids: set[str] = set()
    worker_keys: set[str] = set()
    for raw in raw_graph:
        if not isinstance(raw, Mapping):
            raise MasterRequestError("each work graph node must be an object")
        child_id = str(raw.get("id", "")).strip()
        project = str(raw.get("project", "")).strip()
        chat = str(raw.get("chat", "")).strip()
        repository = str(raw.get("repository", "")).strip()
        branch = str(raw.get("branch", "")).strip()
        criteria = tuple(str(x).strip() for x in raw.get("done_criteria", ()) if str(x).strip())
        if not all((child_id, project, chat, repository, branch)) or not criteria:
            raise MasterRequestError("each node requires id/project/chat/repository/branch/done_criteria")
        issue = raw.get("workstream_issue")
        if issue is not None and (isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0):
            raise MasterRequestError(f"invalid workstream_issue for {child_id}")
        child = ChildGoal(
            child_id=child_id,
            project=project,
            chat=chat,
            repository=repository,
            branch=branch,
            done_criteria=criteria,
            dependencies=tuple(str(x).strip() for x in raw.get("depends_on", ()) if str(x).strip()),
            workstream_issue=issue,
            files=tuple(str(x).strip() for x in raw.get("files", ()) if str(x).strip()),
            scope=str(raw.get("scope", "")).strip(),
        )
        if child_id in child_ids or child.worker_key in worker_keys:
            raise MasterRequestError("child ids and exact Projekt → Chat targets must be unique")
        child_ids.add(child_id)
        worker_keys.add(child.worker_key)
        children.append(child)
    for child in children:
        unknown = set(child.dependencies) - child_ids
        if unknown or child.child_id in child.dependencies:
            raise MasterRequestError(f"invalid dependency for {child.child_id}: {sorted(unknown)}")
    _assert_acyclic(children)
    material = {
        "request_id": request_id,
        "request": request_text,
        "done": done,
        "children": [
            {
                "id": c.child_id, "worker": c.worker_key, "repository": c.repository,
                "branch": c.branch, "done": c.done_criteria, "dependencies": c.dependencies,
                "issue": c.workstream_issue, "files": c.files, "scope": c.scope,
            } for c in children
        ],
    }
    content_hash = _hash(material)
    version = fields.get("GOAL_VERSION") or content_hash[:12]
    return MasterRequest(request_id, version, request_text, tuple(done), tuple(children), source_comment_id or 0, content_hash)


def _assert_acyclic(children: Iterable[ChildGoal]) -> None:
    graph = {child.child_id: child.dependencies for child in children}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise MasterRequestError("work graph contains a dependency cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def newest_master_request(items: Iterable[dict]) -> MasterRequest | None:
    candidates: list[tuple[int, int, str]] = []
    for order, item in enumerate(items):
        body = str(item.get("body", ""))
        if body.lstrip().startswith("MASTER_REQUEST"):
            candidates.append((int(item.get("id") or order), order, body))
    if not candidates:
        return None
    source, _, body = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return parse_master_request(body, source)


@dataclass(frozen=True, slots=True)
class ChatRoute:
    worker_key: str
    transport: str
    destination: str


class RouteRegistry:
    SUPPORTED = frozenset({"github_master", "outbox", "chat_relay"})

    def __init__(self, routes: Mapping[str, ChatRoute] | None = None):
        self.routes = dict(routes or {})

    @classmethod
    def from_json(cls, raw: str) -> "RouteRegistry":
        if not raw.strip():
            return cls()
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise MasterRequestError("CHAT_ROUTES_JSON must be an object")
        routes: dict[str, ChatRoute] = {}
        for worker_key, config in value.items():
            if not isinstance(config, Mapping):
                raise MasterRequestError(f"route {worker_key} must be an object")
            transport = str(config.get("transport", "")).strip()
            destination = str(config.get("destination", "")).strip()
            if transport not in cls.SUPPORTED:
                raise MasterRequestError(f"unsupported route transport: {transport}")
            if not destination:
                raise MasterRequestError(f"route destination missing for {worker_key}")
            if transport == "github_master" and not re.fullmatch(r"issue:[1-9][0-9]*", destination):
                raise MasterRequestError(f"invalid github_master destination for {worker_key}")
            if transport == "chat_relay" and not destination.startswith("chat-route:"):
                raise MasterRequestError(f"invalid chat_relay destination for {worker_key}")
            routes[str(worker_key)] = ChatRoute(str(worker_key), transport, destination)
        return cls(routes)

    def get(self, worker_key: str) -> ChatRoute | None:
        return self.routes.get(worker_key)


class MasterControlPlane:
    """Deterministic Master planner, router and completion aggregator.

    A route proves delivery to a configured transport only. It never claims that
    an arbitrary ChatGPT UI conversation was awakened.
    """

    def __init__(
        self,
        connection,
        routes: RouteRegistry,
        *,
        github_dispatch: Callable[[str, str], None] | None = None,
        relay_dispatch: Callable[[str, str, str], None] | None = None,
        outbox_path: str | Path | None = None,
        reporter: Callable[[str], None] | None = None,
    ):
        self.connection = connection
        self.routes = routes
        self.github_dispatch = github_dispatch
        self.relay_dispatch = relay_dispatch
        self.outbox_path = Path(outbox_path) if outbox_path else None
        self.reporter = reporter
        self._migrate()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS master_requests (
                    request_id TEXT PRIMARY KEY, version TEXT NOT NULL, content_hash TEXT NOT NULL,
                    source_comment_id INTEGER NOT NULL, request_text TEXT NOT NULL,
                    done_criteria TEXT NOT NULL, state TEXT NOT NULL, last_fingerprint TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS master_children (
                    request_id TEXT NOT NULL, child_id TEXT NOT NULL, worker_key TEXT NOT NULL,
                    goal_hash TEXT NOT NULL, dependencies TEXT NOT NULL, state TEXT NOT NULL,
                    blocker TEXT NOT NULL DEFAULT '', dispatched INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(request_id, child_id)
                );
                CREATE TABLE IF NOT EXISTS master_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS master_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)

    def reconcile(self, items: list[dict], worker_rows: Iterable[Mapping]) -> str:
        try:
            request = newest_master_request(items)
        except MasterRequestError as exc:
            self._report("MASTER_BLOCKED", {"state": "BLOCKED", "blockers": ["MASTER_REQUEST_INVALID"], "reason": str(exc)})
            return "BLOCKED"
        if request is None:
            return "IDLE"
        self._upsert_request(request)
        worker_records = {str(row["worker_key"]): row for row in worker_rows}
        child_states: dict[str, str] = {}
        for child in request.children:
            worker_row = worker_records.get(child.worker_key)
            expected_version = f"{request.version}-{child.child_id}"
            worker_version = ""
            if worker_row is not None:
                keys = worker_row.keys() if hasattr(worker_row, "keys") else ()
                if "goal_version" in keys:
                    worker_version = str(worker_row["goal_version"])
            state = (
                str(worker_row["state"])
                if worker_row is not None and (not worker_version or worker_version == expected_version)
                else "ASSIGNED"
            )
            stored = self.connection.execute(
                "SELECT dispatched, state FROM master_children WHERE request_id=? AND child_id=?",
                (request.request_id, child.child_id),
            ).fetchone()
            if stored and stored[0] and worker_row is None:
                state = str(stored[1])
            child_states[child.child_id] = state

        for child in request.children:
            if child_states[child.child_id] in {"DONE", "WAITING_FOR_USER", "BLOCKED", "STALLED"}:
                self._set_child(request, child, child_states[child.child_id], "")
                continue
            if not all(child_states.get(dep) == "DONE" for dep in child.dependencies):
                child_states[child.child_id] = "BLOCKED_DEPENDENCY"
                self._set_child(request, child, "BLOCKED_DEPENDENCY", "")
                continue
            route = self.routes.get(child.worker_key)
            if route is None:
                child_states[child.child_id] = "BLOCKED"
                self._set_child(request, child, "BLOCKED", "CHAT_ROUTE_UNBOUND")
                continue
            if not self._already_dispatched(request, child):
                try:
                    self._dispatch(route, child.prompt(request.version), request, child)
                except Exception as exc:
                    child_states[child.child_id] = "BLOCKED"
                    self._set_child(request, child, "BLOCKED", "CHAT_ROUTE_DELIVERY_FAILED")
                    self._audit(request.request_id, "CHAT_ROUTE_DELIVERY_FAILED", {"child": child.child_id, "reason": str(exc)[:240]})
                    continue
                child_states[child.child_id] = "ASSIGNED"
                self._set_child(request, child, "ASSIGNED", "", dispatched=True)

        state = self._aggregate(child_states)
        with self.connection:
            self.connection.execute(
                "UPDATE master_requests SET state=?, updated_at=CURRENT_TIMESTAMP WHERE request_id=?",
                (state, request.request_id),
            )
        payload = {
            "request_id": request.request_id,
            "goal_version": request.version,
            "state": state,
            "children": child_states,
            "done": f"{sum(value == 'DONE' for value in child_states.values())}/{len(child_states)}",
            "blockers": sorted({
                row[0] for row in self.connection.execute(
                    "SELECT blocker FROM master_children WHERE request_id=? AND blocker<>''", (request.request_id,)
                )
            }),
        }
        self._report("MASTER_DONE" if state == "DONE" else "MASTER_STATUS", payload, request)
        return state

    def _upsert_request(self, request: MasterRequest) -> None:
        current = self.connection.execute(
            "SELECT content_hash, source_comment_id FROM master_requests WHERE request_id=?", (request.request_id,)
        ).fetchone()
        if current and request.source_comment_id < current[1]:
            return
        changed = not current or current[0] != request.content_hash
        with self.connection:
            self.connection.execute(
                """INSERT INTO master_requests(request_id,version,content_hash,source_comment_id,request_text,done_criteria,state)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET
                   version=excluded.version, content_hash=excluded.content_hash,
                   source_comment_id=excluded.source_comment_id, request_text=excluded.request_text,
                   done_criteria=excluded.done_criteria,
                   state=CASE WHEN master_requests.content_hash<>excluded.content_hash THEN 'ASSIGNED' ELSE master_requests.state END,
                   updated_at=CURRENT_TIMESTAMP""",
                (request.request_id, request.version, request.content_hash, request.source_comment_id,
                 request.request, json.dumps(request.done_criteria), "ASSIGNED"),
            )
            if changed:
                self.connection.execute("DELETE FROM master_children WHERE request_id=?", (request.request_id,))
                for child in request.children:
                    self._set_child(request, child, "ASSIGNED", "", commit=False)
        if changed:
            self._audit(request.request_id, "MASTER_PLAN_CREATED", {
                "version": request.version,
                "children": [child.child_id for child in request.children],
            })

    def _set_child(self, request: MasterRequest, child: ChildGoal, state: str, blocker: str, *, dispatched: bool = False, commit: bool = True) -> None:
        sql = """INSERT INTO master_children(request_id,child_id,worker_key,goal_hash,dependencies,state,blocker,dispatched)
                 VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(request_id,child_id) DO UPDATE SET
                 state=excluded.state, blocker=excluded.blocker,
                 dispatched=MAX(master_children.dispatched, excluded.dispatched)"""
        values = (request.request_id, child.child_id, child.worker_key,
                  _hash(child.prompt(request.version)), json.dumps(child.dependencies), state, blocker, int(dispatched))
        if commit:
            with self.connection:
                self.connection.execute(sql, values)
        else:
            self.connection.execute(sql, values)

    def _already_dispatched(self, request: MasterRequest, child: ChildGoal) -> bool:
        row = self.connection.execute(
            "SELECT dispatched, goal_hash FROM master_children WHERE request_id=? AND child_id=?",
            (request.request_id, child.child_id),
        ).fetchone()
        return bool(row and row[0] and row[1] == _hash(child.prompt(request.version)))

    def _dispatch(self, route: ChatRoute, prompt: str, request: MasterRequest, child: ChildGoal) -> None:
        message_id = f"{request.request_id}:{request.version}:{child.child_id}"
        if route.transport == "github_master":
            if not self.github_dispatch:
                raise RuntimeError("github_master dispatcher is not configured")
            self.github_dispatch(route.destination, prompt)
        elif route.transport == "chat_relay":
            if not self.relay_dispatch:
                raise RuntimeError("chat_relay dispatcher is not configured")
            self.relay_dispatch(message_id, route.destination, prompt)
        elif route.transport == "outbox":
            if not self.outbox_path:
                raise RuntimeError("outbox path is not configured")
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            record = sanitize({
                "message_id": message_id, "worker_key": child.worker_key,
                "destination": route.destination, "payload": prompt,
                "delivery_semantics": "relay_pending",
            })
            with self.outbox_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        else:
            raise RuntimeError("unsupported route transport")
        self._audit(request.request_id, "CHILD_DISPATCHED", {
            "child": child.child_id, "worker_key": child.worker_key,
            "transport": route.transport, "message_id": message_id,
        })

    @staticmethod
    def _aggregate(states: Mapping[str, str]) -> str:
        values = set(states.values())
        if values and values == {"DONE"}:
            return "DONE"
        if "WAITING_FOR_USER" in values:
            return "WAITING_FOR_USER"
        if values.intersection({"BLOCKED", "STALLED"}):
            return "BLOCKED"
        return "RUNNING"

    def _audit(self, request_id: str, event_type: str, payload: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO master_audit(request_id,event_type,payload) VALUES(?,?,?)",
                (request_id, event_type, json.dumps(sanitize(payload), sort_keys=True)),
            )

    def _report(self, kind: str, payload: dict, request: MasterRequest | None = None) -> None:
        if not self.reporter:
            return
        fingerprint = _hash({"kind": kind, "payload": payload})
        if request:
            row = self.connection.execute(
                "SELECT last_fingerprint FROM master_requests WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if row and row[0] == fingerprint:
                return
        else:
            row = self.connection.execute(
                "SELECT value FROM master_meta WHERE key='global_report_fingerprint'"
            ).fetchone()
            if row and row[0] == fingerprint:
                return
        body = "\n".join([
            kind,
            f"REQUEST_ID: {payload.get('request_id', '')}",
            f"GOAL_VERSION: {payload.get('goal_version', '')}",
            f"STATE: {payload.get('state', 'BLOCKED')}",
            f"DONE_CRITERIA: {payload.get('done', '0/0')}",
            f"CHILDREN: {json.dumps(payload.get('children', {}), sort_keys=True)}",
            f"BLOCKERS: {json.dumps(payload.get('blockers', []), sort_keys=True)}",
            f"EVIDENCE: {json.dumps({k: v for k, v in payload.items() if k not in {'children', 'blockers'}}, sort_keys=True)}",
            f"FINGERPRINT: {fingerprint}",
        ])
        self.reporter(body)
        if request:
            with self.connection:
                self.connection.execute(
                    "UPDATE master_requests SET last_fingerprint=? WHERE request_id=?",
                    (fingerprint, request.request_id),
                )
        else:
            with self.connection:
                self.connection.execute(
                    "INSERT OR REPLACE INTO master_meta(key,value) VALUES('global_report_fingerprint',?)",
                    (fingerprint,),
                )
