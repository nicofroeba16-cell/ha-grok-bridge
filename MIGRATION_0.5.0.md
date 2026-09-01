# Migration to 1.0.0

The bridge is now a bidirectional `/config` synchronization service.

## Important

- Default initial sync is `ha_to_git`.
- `git_to_ha` is available as an explicit initial mode and manual operation.
- `bidirectional` detects simultaneous local/remote changes and stops instead of overwriting either side.
- Runtime and secret material is excluded by default.
- Secret scanning blocks a push when suspicious secret material is detected.
- Snapshots are created before destructive GitHub -> `/config` operations and before restore.
- Snapshot retention is controlled by `max_snapshots`.
- Manual operations are available through the add-on Ingress endpoints.
- History cleanup is deliberately opt-in via `history_cleanup: true`.
