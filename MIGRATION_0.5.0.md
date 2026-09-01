# Migration to 1.0.0

The bridge is now a bidirectional `/config` synchronization service.

- Initial sync: HA -> GitHub or GitHub -> HA.
- Bidirectional mode detects simultaneous local/remote changes and stops instead of overwriting either side.
- Snapshots are created before destructive remote-to-local operations and restore.
- Snapshot retention is controlled by `max_snapshots`.
- Dry-run is available.
- Exclusion lists and secret scanning protect the live repository.
- Manual operations are exposed through add-on Ingress.
- History cleanup is opt-in for previously transferred secret paths.
