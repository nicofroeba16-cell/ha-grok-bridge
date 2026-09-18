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

A process restart converts an interrupted RUNNING worker back to ASSIGNED for safe reconciliation. READY, DONE, WAITING_FOR_USER and STALLED remain dormant until a materially changed goal is ingested. BLOCKED workers also remain dormant for worker execution; retryable external CI-verification blockers are rechecked during reconciliation without spawning a worker, and a newly GREEN exact-head check can complete the stored goal directly or re-assign it once if technical criteria still remain.

## Dispatch-only mode and chat relay

The production runner may operate without a local worker executor. When
`WORKER_COMMAND` is unset, goals are ingested, deduplicated, reconciled and
reported, but no local AI/code worker is launched. Historical executor-only
failures are re-queued as assignments awaiting the external workstream.

Exact Master-to-chat delivery uses the `chat_relay` route transport. Each
delivery carries a deterministic `message_id` and the same value as the HTTP
`Idempotency-Key`. Route destinations are logical IDs beginning with
`chat-route:`; the relay implementation is responsible for mapping those IDs
to an authorized messaging surface. Non-loopback relay endpoints require HTTPS.

The canonical route registry is `config/chat-routes.json`. A production
installation should set `CHAT_ROUTES_FILE` to that deployed registry and may
set `CHAT_RELAY_URL` plus an optional `CHAT_RELAY_TOKEN`. If no authorized
relay endpoint is configured, `chat_relay` delivery fails closed and is never
treated as delivered.

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

## Documentation Freshness Contract v1

Every material lifecycle transition is published exactly once to the workstream
issue (when configured) and Master Issue #3. A deterministic fingerprint covers
the semantic status payload; timestamps and `SUPERSEDES` metadata do not change
that fingerprint, so restart/poll no-ops do not create status spam. Reports also
carry UTC `TIMESTAMP`, `EVIDENCE`, and `SUPERSEDES` fields.

At startup and before each poll's new assignment ingestion, persisted workers
are compared with canonical status comments already read from Master. Missing
or stale status is re-published without executing the worker. If either issue
write fails after the other succeeds, the worker is persisted as
`BLOCKED` with `DOCUMENTATION_DRIFT`; its report fingerprint is not advanced,
so the next reconciliation retries and converges safely. The daemon continues
serving other workers while this retry remains pending.

## Auto Chat status checkpoint policy

All generated Auto worker GOAL prompts and browser `WORKER_WAKE` payloads carry
`AUTO_POLICY_ID: auto-chat-status-report-and-resume-v1`. A user message that is
only `Status`, `Status?`, `Stand` or `Stand?` is a non-stopping checkpoint:
report concise current state and then continue the already-authorized current
goal without waiting for another Go.

Current-goal resolution is assignment-first, not worker-report-first. A newer
canonical Master assignment therefore supersedes an older terminal predecessor.
For example, an older `WAITING_FOR_USER` export goal cannot suppress a newer
Library-placement successor. Post-send `wake_uncertain` evidence for that
successor counts operationally as RUNNING under the current policy, while
existing live/merge/release/user/device/network/secret gates still remain gates.

Already-active equivalent work is continued without duplicate CI, activation,
restart or rework. A genuinely current `WAITING_FOR_USER` or `BLOCKED` goal
is reported and remains gated; current DONE/READY/STALLED or no-goal workers
remain dormant and invent no work.

Existing registered Auto workers can receive a one-time policy sync through the
Browser-Wake command:

    browser-wake --orchestrator-db <registry.sqlite3> sync-policy

The sync source is the Orchestrator `workers` registry, not the browser route
catalog. A route that merely exists for a legacy/non-Auto chat is never selected
unless that exact canonical worker is already registered. Sync message IDs are
deterministic, so repeated sync requests are idempotent.

Malformed `MASTER_REQUEST` comments are recorded in the Browser-Wake ledger
with source comment ID and a sanitized parse reason. Poll output exposes both
`rejected_master_requests` and the most recent
`rejected_master_request_errors`; advancing the scan cursor no longer makes a
parse rejection disappear silently.

## Least privilege

Use repository-scoped credentials with only the read/write permissions needed for issue status reporting and repository/CI inspection. Keep worker credentials separate from orchestrator credentials. Never place credential values in assignments, logs, issues or committed configuration.

## Runtime hardening

Master ingestion is latest-assignment-wins per exact `Projekt → Chat` worker key. GitHub source comment IDs are the monotonic authority; older assignments cannot supersede a newer persisted source, even across later polls or restarts. Orchestrator reports (`WORKER_STATUS`, `WORKER_DONE`, `WORKER_STALLED`, `INTEGRATION_CONFLICT`) are rejected as assignments.

Daemon execution uses isolated per-worker workspaces under `WORKSPACE_ROOT` (default `/home/vboxuser/.local/share/worker-orchestrator/workspaces`). Existing workspaces must match the assigned repository origin and exact branch and must be clean; mismatches or dirty work fail closed without reset or cleanup. Direct `main`/`master` work is blocked unless the current Goal explicitly approves a privileged base-branch action.

## Master control plane

The optional control plane turns one canonical `MASTER_REQUEST` in Master Issue
#3 into a deterministic dependency graph of exact `Projekt → Chat` goals. It
stores the plan, child state, dispatch ledger and audit events in the existing
SQLite database. A child is dispatched only after every declared dependency is
`DONE`; duplicate dispatch is suppressed across polls and restarts. Global
`DONE` is emitted only when every child is verified `DONE`. `BLOCKED`,
`STALLED`, and `WAITING_FOR_USER` remain visible and cannot be converted into a
false success.

Enable it explicitly:

    MASTER_CONTROL_ENABLED=true
    MASTER_CONTROL_ISSUE=9
    CHAT_ROUTES_JSON='{"Projekt: Example → Chat: Worker":{"transport":"github_master","destination":"issue:3"}}'
    worker-orchestrator --allow-non-dry-run run

Supported route transports are deliberately narrow:

- `github_master` writes the exact child GOAL PROMPT to Master Issue #3 for the
  existing runner execution layer.
- `outbox` appends a deduplicated relay record to `MASTER_OUTBOX`; the record is
  marked `relay_pending`.

An unconfigured target fails closed with `CHAT_ROUTE_UNBOUND`. The package does
not claim to wake an arbitrary ChatGPT UI conversation. Such a wake requires a
separate authenticated, verified relay; unsupported transport names are
rejected during startup.

Canonical request format:

    MASTER_REQUEST
    REQUEST_ID: example-v1
    GOAL_VERSION: v1
    REQUEST: Complete the example work graph
    GLOBAL_DONE_CRITERIA:
    - every child is DONE
    WORK_GRAPH_JSON:
    [
      {
        "id": "worker-a",
        "project": "Example",
        "chat": "Worker A",
        "repository": "owner/repository",
        "branch": "feat/worker-a",
        "workstream_issue": 10,
        "files": ["path/a"],
        "scope": "worker-a",
        "depends_on": [],
        "done_criteria": ["criterion is verified"]
      }
    ]

Material request drift creates a new deterministic plan and reopens affected
children. The audit log is sufficient to reconstruct plan creation, routing,
delivery failures and the message id used for each dispatch.
