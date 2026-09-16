# Runner Worker Orchestrator

Workstream: **Projekt: Worker Orchestrator → Chat: Runner Worker Orchestrator**

Master source: `nicofroeba16-cell/ha-grok-bridge`, Issue #3.

## Purpose

This package is the runner-side execution layer for structured Master assignments. It keeps worker state in SQLite, dispatches a programmatic worker command, reconciles GitHub state, and writes status events back to GitHub.

The Master remains dispatcher and coordinator. Worker implementation stays in the assigned workstream.

## Lifecycle

`IDLE → ASSIGNED → RUNNING → BLOCKED | WAITING_FOR_USER | READY → DONE`

`STALLED` is an escalation state for repeated execution with the same failure and no material progress.

`DONE` requires every explicit Done Criterion to be verified and the exact worker HEAD to have green CI. An unchanged or inactive worker is never considered done.

## Persistent state

SQLite/WAL stores:
- exact project/chat worker key
- repository, branch and workstream issue
- goal hash/version
- lifecycle state
- last HEAD and CI
- blockers and user gate
- progress and completion evidence
- recovery counters and worker session state
- writer locks and audit events

A process restart converts an interrupted RUNNING worker back to ASSIGNED for safe reconciliation. DONE, WAITING_FOR_USER and STALLED remain dormant until a materially changed goal is ingested.

## Assignment format

A structured assignment may use separate fields:

    PROJECT: Example
    CHAT: Worker Name
    REPOSITORY: owner/repo
    BRANCH: worker/branch
    WORKSTREAM_ISSUE: 12
    GOAL_VERSION: optional-version
    SCOPE: stable-scope
    FILES: path/a.py,path/b.py
    APPROVED_ACTIONS: optional-explicit-actions
    DONE_CRITERIA:
    - criterion one
    - criterion two

The canonical identity line is also accepted:

    Projekt: Example → Chat: Worker Name

If GOAL_VERSION is omitted, a SHA-256 hash of the material assignment becomes its durable version.

## Safety model

Dry-run is the default. The orchestrator does not contain automatic merge, deployment, restart, device, network, runtime, destructive, release, key or secret mutation logic.

When a worker reports an action that requires user approval and that action is not explicitly approved for the current goal, the state becomes WAITING_FOR_USER.

Repository access is allowlisted. Worker subprocesses receive only a minimal environment plus names explicitly configured in WORKER_ENV_ALLOWLIST. The GitHub credential used by the orchestrator is not inherited by worker subprocesses. Secret-like output is redacted before persistence/reporting.

Duplicate writers on the same branch, overlapping declared files, or the same declared scope are stopped and reported as INTEGRATION_CONFLICT.

## Install and test

From `worker_orchestrator/`:

    python -m venv .venv
    . .venv/bin/activate
    python -m pip install -e .
    python -m unittest discover -s tests -v

## Single reconciliation

Required configuration includes a GitHub credential, a programmatic worker command and an explicit repository allowlist.

    worker-orchestrator once

Dry-run remains enabled unless `--allow-non-dry-run` is explicitly supplied. Approval-gated actions are still handled separately by the goal contract.

## Daemon mode

    worker-orchestrator --poll-seconds 300 run --webhook-host 127.0.0.1 --webhook-port 8787

GitHub webhook events wake reconciliation immediately; the poll interval is the fallback.

Binding the webhook listener outside loopback requires a configured webhook signing secret.

SIGINT or SIGTERM cleanly stops the daemon. The SQLite database is the recovery source on the next start.

## Least privilege

Use repository-scoped credentials with only the read/write permissions needed for issue status reporting and repository/CI inspection. Keep worker credentials separate from orchestrator credentials. Never place credential values in assignments, logs, issues or committed configuration.
