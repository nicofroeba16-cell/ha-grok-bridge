# Instructions for AI agents using HA File Sync Bridge

## Purpose

This repository contains **HA File Sync Bridge**, a Home Assistant add-on that synchronizes the Home Assistant `/config` tree with a GitHub repository and can automatically deploy remote GitHub changes back to Home Assistant.

The current bridge version is **1.0.8**.

## Repositories

- Add-on source: `nicofroeba16-cell/ha-grok-bridge`
- Home Assistant live configuration repository: `nicofroeba16-cell/ha-grok-bridge-live`
- Deployment branch: `main`

## AI operating model

When an AI agent is asked to change the Home Assistant project, prefer this workflow:

1. Inspect the current repository state before changing anything.
2. Make the required change in the appropriate file.
3. Commit the change to the intended Git branch.
4. For Home Assistant configuration changes, use `ha-grok-bridge-live` as the GitHub source repository.
5. The installed HA File Sync Bridge 1.0.8 detects a new remote commit and deploys permitted files to `/config` automatically when `deploy_on_remote_change` is enabled.
6. Verify the resulting GitHub state and, when HA runtime access is available, verify the deployment status before declaring the change complete.

## File deployment

The bridge supports universal file writes and nested directories. A repository-relative path maps to the same relative path below `/config`.

Examples:

- `configuration.yaml` -> `/config/configuration.yaml`
- `custom_components/example/__init__.py` -> `/config/custom_components/example/__init__.py`
- `www/app/index.html` -> `/config/www/app/index.html`
- `packages/example.yaml` -> `/config/packages/example.yaml`

Directories are created automatically when required.

## Security rules

Never intentionally commit or deploy credentials, private keys, tokens, or Home Assistant runtime data.

The bridge excludes sensitive/runtime paths including, by default:

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

The bridge also performs secret scanning for supported text/configuration formats.

Do not bypass these protections merely to make a deployment succeed.

## Path safety

Deployment paths must remain relative to `/config`. Never use absolute paths or path traversal such as `../`.

Do not use symlinks or repository content to escape the `/config` deployment boundary.

## Autonomous deployment

Version 1.0.8 supports:

- GitHub remote-change detection
- automatic GitHub -> `/config` deployment
- arbitrary permitted file types
- arbitrary permitted nested directories
- automatic directory creation
- pre-deployment security scanning
- pre-deployment snapshots
- post-deployment integrity verification
- rollback on deployment errors
- commit-based deployment state
- bidirectional synchronization and conflict detection

The relevant configuration defaults include:

```yaml
deploy_on_remote_change: true
rollback_on_error: true
```

## Important distinction

GitHub changes are the **source of a deployment**, but an AI can only cause a live Home Assistant deployment if:

- the AI has permission to modify the GitHub repository, and
- the Home Assistant instance is running HA File Sync Bridge 1.0.8 (or newer), and
- the bridge can reach and pull the configured GitHub repository.

GitHub commits alone do not provide an AI with direct shell access to the Home Assistant host.

## Recommended AI behavior

For normal project work:

- Work from the repository's existing conventions.
- Read the relevant file before replacing it.
- Keep changes minimal and targeted.
- Do not invent runtime state that cannot be verified.
- Never expose secrets in responses, commits, logs, or test fixtures.
- Do not treat `.cloud`, `.storage`, `.ssh`, databases, logs, or credentials as normal project source files.
- After changing code, inspect the resulting diff and commit state.
- For changes intended for HA, state the exact repository-relative paths changed.

## Deployment verification

A successful Git commit is not by itself proof that Home Assistant applied the change.

When runtime access exists, verify:

1. the bridge detected the new commit,
2. security scanning passed,
3. deployment completed,
4. integrity verification passed,
5. no rollback occurred,
6. the expected file exists at the expected `/config/...` path.

If runtime access does not exist, report that limitation instead of claiming the HA deployment was verified.

## API capabilities exposed by the bridge

The bridge provides HTTP endpoints for status/synchronization and universal file operations. The write interface accepts either:

```json
{"path":"folder/file.txt","content":"text"}
```

or:

```json
{"directory":"folder/subfolder","filename":"file.txt","content":"text"}
```

Binary content can be supplied using base64.

Directory browsing is available through `/files` and `/browse`.

These interfaces are still subject to the same `/config` path and sensitive-file protections.

## Version awareness

Before making changes that depend on bridge behavior, inspect the actual installed/source version. This document describes the 1.0.8 contract and should be updated whenever the deployment contract changes materially.
