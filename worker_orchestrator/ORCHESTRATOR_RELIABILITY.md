# Orchestrator Reliability v1

Goal version: `orchestrator-reliability-source-tests-v1`

This dossier describes source/test hardening only. It does not activate services,
change production registries or ledgers, replay deliveries, deploy, merge, or touch
Home Assistant/device/runtime state.

## Terminal-state mirroring

Canonical worker terminal states are `READY`, `DONE`, `WAITING_FOR_USER`, and
`BLOCKED`. The existing dual-destination reporter writes the same semantic
fingerprint to the worker issue and Master Issue #3. Duplicate reports with the
same fingerprint are suppressed.

A partial write raises `DocumentationDriftError`. The original semantic report is
stored as pending documentation evidence, the local worker is blocked rather than
falsely completed, and reconciliation targets only the missing/stale destination.
A successful reconciliation restores the intended state and clears the pending
documentation marker without re-executing the worker.

## Registry classification

`classify_registry_rows` classifies matching rows as `MISSING`, `LEGACY_ONLY`,
`ACTIVE`, `ACTIVE_WITH_DUPLICATES`, or `AMBIGUOUS_DUPLICATE`.
Legacy versions are evidence only and cannot supply the current goal state.
Equivalent duplicate current rows collapse deterministically. Conflicting current
rows fail closed with `REGISTRY_DUPLICATE_AMBIGUOUS` and are not dispatched.

Route JSON parsing rejects duplicate keys. Browser routes additionally reject two
logical route keys that resolve to the same concrete ChatGPT conversation URL.
Missing routes retain the existing fail-closed `CHAT_ROUTE_UNBOUND` behavior.

## UNCERTAIN delivery recovery

An interrupted or post-send-indeterminate browser delivery is `UNCERTAIN`.
`WakeLedger.claim` never automatically retries an `UNCERTAIN` message.
`evaluate_delivery` exposes sanitized evidence, attempt count, retry semantics,
and the required operator action. `uncertain_deliveries` provides deterministic
read-only enumeration for recovery tooling.

Only `FAILED_PRE_SEND` is marked retryable by the ledger. This preserves
at-most-once safety when a send may already have occurred.

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

`FAILED_PRE_SEND`, `IN_FLIGHT`, `UNCERTAIN`, unverified `DELIVERED`, stale
goal versions, ambiguous/wrong routes, and terminal states never promote
`RUNNING`. Duplicate verified evidence is idempotent. Browser-verified external
`RUNNING` workers are also preserved across an Orchestrator daemon restart
instead of being incorrectly requeued as local interrupted execution.

## Verification contract

The reliability tests cover:
- idempotent dual-destination mirroring for all four terminal states;
- partial child/Master write recovery without false completion;
- legacy and equivalent/conflicting duplicate row classification;
- missing, duplicate-key, and ambiguous-destination route failures;
- persistent `UNCERTAIN` evidence with no automatic retry;
- positive verified wake promotion, negative delivery states, wrong-route rejection,
  changed-goal protection, duplicate idempotency, publication retry, and restart
  preservation of externally running workers.

Verification must be run from the exact candidate HEAD with:
`PYTHONPATH=worker_orchestrator/src python3 -m unittest discover -s worker_orchestrator/tests -v`
and `git diff --check`.

All changed files for this goal must remain under `worker_orchestrator/**`.
