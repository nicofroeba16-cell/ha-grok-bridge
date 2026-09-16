from __future__ import annotations

import re
from typing import Iterable

from .models import Goal


FIELD = re.compile(r"^\s*([A-Z_ ]{2,32})\s*:\s*(.*?)\s*$")


def _criteria(lines: list[str]) -> tuple[str, ...]:
    items: list[str] = []
    active = False
    for raw in lines:
        line = raw.strip()
        key = line.upper().replace("-", "_")
        if "DONE_CRITERIA" in key or "DONE_KRITERIEN" in key:
            active = True
            continue
        if active and line.startswith("-"):
            item = line[1:].strip()
            if item:
                items.append(item)
            continue
        if active and items and line and ":" in line:
            break
    return tuple(items)


def parse_goal_text(text: str, *, source_comment_id: int | None = None) -> Goal | None:
    lines = text.splitlines()
    fields: dict[str, str] = {}
    for line in lines:
        match = FIELD.match(line)
        if match:
            fields[match.group(1).strip().upper().replace(" ", "_")] = match.group(2).strip()

    project = fields.get("PROJECT", "")
    chat = fields.get("CHAT", "")
    repository = fields.get("REPOSITORY", "")
    branch = fields.get("BRANCH", "")
    done = _criteria(lines)
    if not project or not chat or not done:
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
        explicit_version=fields.get("GOAL_VERSION") or None,
        files=tuple(x.strip() for x in fields.get("FILES", "").split(",") if x.strip()),
        scope=fields.get("SCOPE", ""),
        approved_actions=tuple(x.strip().lower() for x in fields.get("APPROVED_ACTIONS", "").split(",") if x.strip()),
        source_comment_id=source_comment_id,
    )


def parse_goals(items: Iterable[dict]) -> list[Goal]:
    goals: list[Goal] = []
    for item in items:
        goal = parse_goal_text(item.get("body") or "", source_comment_id=item.get("id"))
        if goal:
            goals.append(goal)
    return goals
