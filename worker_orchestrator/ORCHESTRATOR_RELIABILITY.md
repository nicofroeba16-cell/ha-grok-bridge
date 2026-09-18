# Orchestrator Reliability v1

Goal version: `orchestrator-reliability-source-tests-v1`

This dossier describes source/test hardening only. It does not activate services,
change production registries or ledgers, replay deliveries, deploy, merge, or touch
Home Assistant/device/runtime state.

## Canonical documentation two-phase commit

Policy `auto-doc-sync-enforcement-v1` makes canonical documentation part of the
state machine rather than best-effort chat prose. Every material status is queued
in a durable SQLite outbox using worker + current goal version + desired state +
semantic evidence fingerprint. Phase A writes and read-backs the canonical
workstream issue. Only after that succeeds may Phase B write and read-back the
Master mirror. The exposed states are `DOC_SYNC_PENDING`,
`DOC_SYNC_WORKSTREAM_VERIFIED`, `DOC_SYNC_MASTER_PENDING`, `DOC_SYNC_VERIFIED`,
`DOC_SYNC_BLOCKED`, and stale marker `DOC_SYNC_REQUIRED`.

Terminal `READY`/`DONE` evidence is kept provisional until both canonical
destinations are durable. A workstream write failure blocks documentation without
claiming terminal completion. A Master-only failure preserves Phase A and retries
only the mirror after restart/reconciliation; product work is not rerun. Duplicate
semantic retries reuse the same outbox identity.

## Registry classification

`classify_registry_rows` classifies matching rows as `MISSING`, `LEGACY_ONLY`,
`ACTIVE`, `ACTIVE_WITH_DUPLICATES`, or `AMBIGUOUS_DUPLICATE`.
Legacy versions are evidence only and cannot supply the current goal state.
Equivalent duplicate current rows collapse deterministically. Conflicting current
rows fail closed with `REGISTRY_DUPLICATE_AMBIGUOUS` and are not dispatched.

Route JSON parsing rejects duplicate keys. Browser routes additionally reject two
logical route keys that resolve to the same concrete ChatGPT conversation URL.
Missing routes retain the existing fail-closed `CHAT_ROUTE_UNBOUND` behavior.

## Browser send commit / interrupted recovery

Browser-Wake now records a message-id-bound commit-marker path when claiming a
send. The browser helper atomically writes `{state: SEND_COMMITTED}` immediately
before its single Send click. Restart recovery therefore distinguishes:

- marker-aware interruption with no valid marker: `FAILED_PRE_SEND`, retryable;
- valid send-commit marker: `UNCERTAIN`, never blindly retried;
- legacy in-flight row without commit evidence: `UNCERTAIN` fail-closed.

The helper also detects submitted naked `WORKER_WAKE`/`MASTER_WAKE` prefix turns.
A prefix-only turn never counts as success and blocks automatic resend. Composer
recovery may clear only a draft containing the exact current WAKE_ID and expected
wake prefix; arbitrary user drafts fail closed.

## Confirmed wake -> RUNNING

Goal `orchestrator-confirmed-wake-running-v1` adds a separate positive-delivery
contract. A browser helper may claim verified delivery only after all of these are
true: the exact intended conversation path loaded, the user wake turn appeared,
the assistant turn completed without scraping its content, a reload completed,
and the same wake turn persisted in that same conversation.

The Browser-Wake ledger persists this verified receipt before publishing a
`BROWSER_WAKE_DELIVERY` source event to Master Issue #3. Publication failure is
retryable from the ledger without re-sending the wake. The Orchestrator accepts
only current-goal events with the full persisted/destination verification evidence
and promotes only `ASSIGNED`/eligible idle state to `RUNNING`.

The later shared Auto policy `auto-chat-status-report-and-resume-v1` refines
operational status resolution without weakening delivery safety: `FAILED_PRE_SEND`
and `IN_FLIGHT` remain non-running/activating, while an explicitly classified
post-send `wake_uncertain` for the newest current goal is reported operationally
as `RUNNING` with `activation_confirmed=false`. Positive verification upgrades
provenance to confirmed `wake_verified`. Older terminal predecessor evidence
cannot suppress a newer assigned successor, while a terminal state for that same
current goal remains protected. Duplicate evidence stays idempotent.


## Existing-tab / SPA hydration recovery

The sender resolves title routes in a disposable resolver page, then prefers an
already-open exact conversation page only when it is hydrated with a visible
composer. It never navigates an unrelated healthy ChatGPT tab through the target
URL. If no exact hydrated page exists, fresh-page recovery is bounded and requires
exact conversation identity plus composer hydration before the idle gate and any
composer mutation. Shell-only/no-composer, auth/external redirect, and route
mismatch are distinct pre-send failures; shell-only remains safe-to-retry.

Atomic multiline insertion remains one DOM operation. ProseMirror paragraph
readback reconstructs logical newline-delimited payload text, and long collapsed
user messages are verified from their message-content node rather than UI toggle
labels.

## Boot / display readiness

`run_browser_wake_chrome.sh` performs a bounded X11 readiness loop before Chrome
exec. It refreshes Mutter Xwayland authority and probes with `xset`/`xdpyinfo`,
logging `DISPLAY_READINESS_WAIT`, `DISPLAY_READY`, or
`BLOCKED_DISPLAY_NOT_READY`. This prevents fast repeated boot failures from
exhausting systemd start limits while preserving existing supervision. The
readiness-only test mode is fixture-only and does not launch Chrome.

## Visual-media scope policy

Policy `auto-media-ui-only-v1` defaults every goal to `UI_VISUAL_SCOPE=false`.
Automatic visual-media work is permitted only when `UI_VISUAL_SCOPE=true` or
`VISUAL_MEDIA_REQUIRED=true`. Lifecycle markers such as `SIMULATION_READY` alone
never trigger screenshots, videos, GIFs, or Library media. Reliability/runtime
backend goals therefore use technical evidence only.

## Verification contract

The reliability tests cover:
- durable two-phase workstream/Master documentation with restart-safe partial recovery;
- terminal rejection until `DOC_SYNC_VERIFIED`, stale-doc repair without product rerun;
- legacy and equivalent/conflicting duplicate row classification;
- missing, duplicate-key, and ambiguous-destination route failures;
- marker-aware pre-send retry vs post-commit `UNCERTAIN`, naked-prefix/draft fail-closed recovery;
- existing exact hydrated-tab reuse and shell/no-composer pre-send deferral;
- bounded boot/display readiness success and fail-closed timeout simulations;
- explicit non-UI media policy where `SIMULATION_READY` does not imply media;
- positive verified wake promotion, negative delivery states, wrong-route rejection,
  changed-goal protection, duplicate idempotency, publication retry, and restart
  preservation of externally running workers.

Verification must be run from the exact candidate HEAD with:
`PYTHONPATH=worker_orchestrator/src python3 -m unittest discover -s worker_orchestrator/tests -v`
and `git diff --check`.

All changed files for this goal must remain under `worker_orchestrator/**`.
