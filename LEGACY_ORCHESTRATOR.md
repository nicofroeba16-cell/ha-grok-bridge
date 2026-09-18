# Legacy Orchestrator Boundary

Status: migration boundary only; no deletion in this change.

## Canonical ownership

- Home Assistant File Sync Bridge/add-on: `nicofroeba16-cell/ha-grok-bridge`
- Master/Worker Orchestrator, Browser Wake and control-plane runtime: `nicofroeba16-cell/master-orchestration`

## Migration state

The orchestration source was migrated and exact-head validated in `master-orchestration`. Production Browser Wake and Worker Orchestrator were subsequently unified on the same immutable `master-orchestration` runtime release.

The `worker_orchestrator/` subtree in this repository is now a legacy snapshot. It must not receive new product changes.

## Removal gate

The legacy subtree may be removed only after all of the following are verified:

1. no production service, runtime pointer, environment, installer or deployment script references this repository as the orchestration source;
2. no active worker or CI workflow depends on this subtree;
3. documentation and bootstrap references point to `master-orchestration`;
4. rollback evidence for the migrated runtime is retained;
5. a separate explicit removal/merge approval is given.

This document does not authorize deletion, merge, release, service restart, or runtime mutation.
