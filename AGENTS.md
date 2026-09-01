# Instructions for AI agents using HA File Sync Bridge

## Purpose

This repository contains **HA File Sync Bridge**, a Home Assistant add-on that synchronizes the permitted Home Assistant `/config` tree with a GitHub repository and automatically deploys permitted remote changes back to `/config`.

The current bridge version is **1.10**.

## Repositories

- Add-on source: `nicofroeba16-cell/ha-grok-bridge`
- Home Assistant live configuration repository: `nicofroeba16-cell/ha-grok-bridge-live`
- Deployment branch: `main`

## AI operating model

For Home Assistant project changes, an AI agent should work through GitHub rather than requiring manual shell commands on the HA host:

1. Inspect the current repository state.
2. Change the required file in `ha-grok-bridge-live`.
3. Commit the change to the intended branch.
4. Use a repository-relative path; that path maps to the same relative path below `/config`.
5. The installed Bridge 1.10 polls GitHub automatically and detects a new remote commit.
6. The bridge security-scans, snapshots, deploys, verifies hashes, and rolls back on failure when enabled.

## Path-preserving deployment

A GitHub path is the deployment path. Examples:

- `configuration.yaml` -> `/config/configuration.yaml`
- `automations/test.yaml` -> `/config/automations/test.yaml`
- `custom_components/example/__init__.py` -> `/config/custom_components/example/__init__.py`
- `www/app/index.html` -> `/config/www/app/index.html`
- `packages/example.yaml` -> `/config/packages/example.yaml`

Nested directories are created automatically. Permitted arbitrary file types are supported; deployment is not limited to YAML.

## Bidirectional behavior

With `sync_mode: bidirectional`:

- GitHub commit -> automatic GitHub -> `/config` deployment.
- `/config` change -> automatic `/config` -> GitHub commit/push.
- If both sides changed since the last known synchronized commit, the bridge reports a conflict instead of silently overwriting either side.

Remote deployment is enabled by default with `deploy_on_remote_change: true`.

## Security and exclusions

Never intentionally commit or deploy credentials, private keys, tokens, or Home Assistant runtime data.

The bridge excludes, by default:

- `.storage/`
- `.cloud/`
- `.ssh/`
- `.cache/`
- `secrets.yaml`
- Home Assistant database files
- Home Assistant log files
- `tts/`
- `media/`
- `backups/`
- files ending in `.passphrase`, `.pem`, `.key`, `.p12`, or `.pfx`

The bridge performs secret scanning on deployable text/configuration files. Do not bypass these protections.

## Path safety

All deployment and write paths must be relative to `/config` and must never contain `..`, absolute path components, or a path that resolves outside `/config`.

The HTTP write API accepts either:

```json
{"path":"folder/file.txt","content":"text"}
```

or:

```json
{"directory":"folder/subfolder","filename":"file.txt","content":"text"}
```

Binary data can be supplied as base64. `/files` and `/browse` expose permitted directory contents.

## Autonomous deployment guarantees

Bridge 1.10 is designed as an autonomous GitHub-driven deployment bridge with:

- GitHub remote-change detection
- path-preserving GitHub -> `/config` deployment
- arbitrary permitted file types
- arbitrary permitted nested directories
- automatic directory creation
- secret scanning before deployment
- snapshots before deployment
- integrity/hash verification after deployment
- rollback on deployment errors
- commit-based deployment state
- bidirectional synchronization
- conflict detection
- protection against excluded/runtime content being synchronized

Default safety settings:

```yaml
deploy_on_remote_change: true
rollback_on_error: true
auto_reload: false
```

`auto_reload` is deliberately disabled by default; deploying a file does not imply restarting Home Assistant or reloading an integration unless that behavior is explicitly implemented and verified.

## Important distinction

An AI does not receive direct shell access to Home Assistant merely because this repository exists. Autonomous live deployment requires:

- AI permission to modify the configured GitHub repository,
- Bridge 1.10 (or newer) running on the Home Assistant instance, and
- the bridge being able to reach and pull the configured GitHub repository.

When those conditions hold, a normal AI workflow is simply: **change/commit the desired GitHub path and let the bridge deploy it automatically**.

## Verification

A GitHub commit alone is not proof of live deployment. When runtime status is available, verify that the bridge detected the commit, security scanning passed, deployment completed, integrity verification passed, no rollback occurred, and the expected `/config/...` path exists.

Never claim live HA deployment was verified when runtime access was not available.

## AI behavior

- Read relevant files before replacing them.
- Keep changes targeted.
- Preserve existing conventions.
- Use exact repository-relative paths.
- Never expose secrets.
- Treat `.cloud`, `.storage`, `.ssh`, `.cache`, databases, logs, and credentials as runtime/sensitive content, not normal source files.
- After a change, inspect the resulting diff and commit state.
- For HA changes, report the exact repository-relative paths changed.

## Version awareness

Inspect the actual source/installed version before relying on bridge behavior. Update this document whenever the deployment contract changes materially.
