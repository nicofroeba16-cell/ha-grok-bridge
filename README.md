# HA File Sync Bridge

Home Assistant add-on for controlled bidirectional synchronization of `/config` with GitHub.

1. GitHub -> /config synchronization
2. Conflict detection
3. Snapshot restore
4. Dry-run
5. Configurable exclusions
6. Snapshot retention and cleanup
7. Health/status
8. Sync locking
9. Correct deletion handling
10. Secret scanning
11. Initial sync direction
12. Manual sync via Ingress
13. Add-on status interface
14. Opt-in history cleanup for accidentally committed secret paths

## Repository boundary

`ha-grok-bridge` is the canonical Home Assistant File Sync Bridge/add-on repository.

The historical `worker_orchestrator/` subtree is retained temporarily for migration traceability only. It is **legacy/read-only** and is no longer the canonical development or production source for Master/Worker orchestration, Browser Wake, or control-plane runtime tooling.

Canonical orchestration source: `nicofroeba16-cell/master-orchestration`.

Do not add new orchestration features here. Removal of the legacy subtree requires a separate migration/removal gate after global reference verification.
