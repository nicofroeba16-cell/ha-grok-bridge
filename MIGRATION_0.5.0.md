# Controlled migration 0.3.x → 0.5.0

This branch is a review-only migration branch. It must not be merged until the add-on image has been built and tested on Home Assistant.

## Runtime

The legacy command poller is removed. 0.5.0 uses `addon/file_bridge.py` and structured requests in the separate configuration repository.

## Request model

`bridge/request.json` supports only:

- `snapshot`
- `validate`
- `deploy`
- `rollback`
- `status`

There is no shell-command field and the bridge does not execute commands supplied by Git.

## Deployment gate

`validate` and `deploy` require a 40-character hexadecimal commit SHA and verify that it is an ancestor of the configured `main` branch. Only explicitly allowed paths and scopes are synchronized. YAML files are parsed before deployment.

## Recovery

The affected allowed files are backed up before deployment. Files removed from the candidate are included in the affected set. A failed `ha core check` restores the backup. Successful deployments are recorded as `last_known_good` for manual rollback.

## Branches

The configuration repository is expected to use `main`, `bridge-control`, `live-snapshot`, and `bridge-status`. This PR does not create or modify those branches in the configuration repository.

## Cutover order

1. Review and merge this PR only after code/container checks pass.
2. Build the 0.5.0 add-on image.
3. Keep the add-on stopped during initial verification.
4. Bootstrap the configuration repository control/snapshot/status branches.
5. Create a harmless `status` request and verify the status branch.
6. Create a `validate` request for a known-good `main` commit.
7. Only after validation succeeds, perform the first scoped deploy.
8. Verify `ha core check` and the status record.

No Home Assistant runtime or configuration is changed by this pull request itself.
