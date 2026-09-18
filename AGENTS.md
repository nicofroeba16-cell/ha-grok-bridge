# Instructions for AI agents using HA File Sync Bridge

## Purpose

This repository contains **HA File Sync Bridge**, a Home Assistant add-on that can synchronize a permitted Home Assistant `/config` tree with an explicitly configured GitHub repository and can deploy permitted remote changes back to `/config`.

The current bridge version is **1.13**.

## Repositories

- Add-on source: `nicofroeba16-cell/ha-grok-bridge`
- Canonical Home Assistant configuration source: `nicofroeba16-cell/HA-CONFIG`
- Canonical orchestration source: `nicofroeba16-cell/master-orchestration`
- Deployment branch: `main`

## AI operating model

For Home Assistant project changes, an AI agent should use the canonical source repository and the controlled deployment workflow rather than treating a runtime mirror as source of truth:

1. Inspect the current repository state.
2. Change the required file in `HA-CONFIG` on the intended branch.
3. Commit and verify the change.
4. Use the controlled deployment path for applying approved changes to `/config`.
5. Do not assume the bridge is enabled or configured; its `config_repo` must be set explicitly before synchronization can occur.
6. Verify live state separately from source state.

## Path-preserving deployment

A repository-relative path maps to the same relative path below `/config` when a deployment path is explicitly used. Examples:

- `configuration.yaml` -> `/config/configuration.yaml`
- `automations/test.yaml` -> `/config/automations/test.yaml`
- `custom_components/example/__init__.py` -> `/config/custom_components/example/__init__.py`
- `www/app/index.html` -> `/config/www/app/index.html`
- `packages/example.yaml` -> `/config/packages/example.yaml`

Nested directories can be created automatically. Permitted arbitrary file types are supported; deployment is not limited to YAML.

## Bridge behavior

If the bridge is explicitly configured and running with `sync_mode: bidirectional`:

- GitHub commit -> permitted GitHub -> `/config` deployment.
- `/config` change -> permitted `/config` -> GitHub commit/push.
- If both sides changed since the last known synchronized commit, the bridge reports a conflict instead of silently overwriting either side.

The current repository default for `config_repo` is empty. This is intentional and fail-closed: no synchronization repository should be assumed implicitly.

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

## Safety guarantees

When explicitly configured and enabled, Bridge 1.13 provides:

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

Default safety settings include:

```yaml
deploy_on_remote_change: true
rollback_on_error: true
auto_reload: false
```

`auto_reload` is deliberately disabled by default; deploying a file does not imply restarting Home Assistant or reloading an integration unless that behavior is explicitly implemented and verified.

## Important distinction

An AI does not receive direct shell access to Home Assistant merely because this repository exists. Live deployment requires explicit authorization, a configured deployment path, and separate verification of runtime state.

The bridge must not be treated as the canonical source repository. `HA-CONFIG` remains the canonical Home Assistant configuration source.

## Verification

A GitHub commit alone is not proof of live deployment. When runtime status is available, verify the actual `/config` state, relevant hashes, deployment result, and rollback/error state.

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
