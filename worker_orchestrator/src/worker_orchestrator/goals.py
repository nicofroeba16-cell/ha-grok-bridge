from __future__ import annotations

import re
from typing import Iterable

from .models import Goal


FIELD = re.compile(r"^\s*([A-Za-zÄÖÜäöü_ -]{2,40})\s*:\s*(.*?)\s*$")
REPORT_MARKERS = {"WORKER_STATUS", "WORKER_DONE", "WORKER_STALLED", "INTEGRATION_CONFLICT"}


def _canonical_identifier(text: str) -> tuple[str, str]:
    for raw in text.splitlines():
        line = raw.replace("**", "").strip()
        if line.lower().startswith("projekt:") and "→" in line:
            left, right = line.split("→", 1)
            if right.strip().lower().startswith("chat:"):
                project = left.split(":", 1)[1].strip()
                chat = right.split(":", 1)[1].strip()
                if project and chat:
                    return project, chat
    return "", ""


def _normalize_field(name: str) -> str:
    return name.strip().upper().replace("-", "_").replace(" ", "_")



def _bool_field(value: str | None) -> bool:
    return str(value or '').strip().lower() in {"1", "true", "yes"}


def _criteria(lines: list[str]) -> tuple[str, ...]:
    items: list[str] = []
    active = False
    for raw in lines:
        line = raw.strip()
        key = _normalize_field(line.rstrip(":"))
        if "DONE_CRITERIA" in key or "DONE_KRITERIEN" in key:
            active = True
            continue
        if active and line.startswith("-"):
            item = line[1:].strip()
            if item:
                items.append(item)
            continue
        if active and re.match(r"^\d+[.)]\s+", line):
            item = re.sub(r"^\d+[.)]\s+", "", line).strip()
            if item:
                items.append(item)
            continue
        if active and items and line and ":" in line:
            break
    return tuple(items)


def parse_goal_text(text: str, *, source_comment_id: int | None = None) -> Goal | None:
    lines = text.splitlines()
    normalized_lines = {line.replace("**", "").strip().upper() for line in lines}
    if normalized_lines.intersection(REPORT_MARKERS):
        return None
    fields: dict[str, str] = {}
    for line in lines:
        match = FIELD.match(line)
        if match:
            fields[_normalize_field(match.group(1))] = match.group(2).strip()

    canonical_project, canonical_chat = _canonical_identifier(text)
    field_project = fields.get("PROJECT") or fields.get("PROJEKT") or ""
    field_chat = fields.get("CHAT") or ""
    # Explicit headers identify the assignment. A goal body may legitimately
    # mention another exact Projekt → Chat route, which must not take over the
    # assignment's own identity.
    if field_project and field_chat:
        project, chat = field_project, field_chat
    else:
        project = canonical_project or field_project
        chat = canonical_chat or field_chat
    repository = fields.get("REPOSITORY") or fields.get("REPO") or ""
    branch = fields.get("BRANCH", "")
    done = _criteria(lines)
    assignment_marker = bool(fields.get("GOAL_VERSION") or fields.get("GOAL_VERSION_HASH")) or any("GOAL PROMPT" in x.upper() for x in lines) or bool(repository and branch)
    if not assignment_marker or not project or not chat or not done:
        return None

    issue_raw = fields.get("WORKSTREAM_ISSUE") or fields.get("ISSUE")
    issue = int(issue_raw.lstrip("#")) if issue_raw and issue_raw.lstrip("#").isdigit() else None

    return Goal(
        project=project,
        chat=chat,
        repository=repository,
        branch=branch,
        prompt=text.strip(),
        done_criteria=done,
        workstream_issue=issue,
        explicit_version=fields.get("GOAL_VERSION") or fields.get("GOAL_VERSION_HASH") or None,
        files=tuple(x.strip() for x in fields.get("FILES", "").split(",") if x.strip()),
        scope=fields.get("SCOPE", ""),
        approved_actions=tuple(
            x.strip().lower()
            for x in fields.get("APPROVED_ACTIONS", "").split(",")
            if x.strip()
        ),
        ui_visual_scope=_bool_field(fields.get("UI_VISUAL_SCOPE")),
        visual_media_required=_bool_field(fields.get("VISUAL_MEDIA_REQUIRED")),
        source_comment_id=source_comment_id,
    )


def parse_goals(items: Iterable[dict]) -> list[Goal]:
    """Return only the newest assignment per worker key for this reconciliation."""
    newest: dict[str, tuple[int, int, Goal]] = {}
    for order, item in enumerate(items):
        goal = parse_goal_text(item.get("body") or "", source_comment_id=item.get("id"))
        if not goal:
            continue
        source = goal.source_comment_id if goal.source_comment_id is not None else order
        current = newest.get(goal.key)
        if current is None or (source, order) > (current[0], current[1]):
            newest[goal.key] = (source, order, goal)
    return [entry[2] for entry in sorted(newest.values(), key=lambda x: (x[0], x[1]))]
