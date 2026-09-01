# HA File Sync Bridge 0.5.0

The add-on keeps the existing Home Assistant slug `ha_grok_bridge` but replaces the legacy command poller with a structured Git file workflow.

## Workflow

- `main` = approved desired configuration
- `bridge-control` = `bridge/request.json` structured request
- `live-snapshot` = allowed files exported from `/config`
- `bridge-status` = `bridge/status.json` result of the latest request

Supported actions are `snapshot`, `validate`, `deploy`, `rollback`, and `status`.

`deploy` accepts only a full 40-character commit SHA that is an ancestor of the configured `main` branch. Only explicitly allowed configuration paths are synchronized. Secrets, `.storage`, `.cloud`, and Home Assistant database files are excluded.

Before a deployment the affected files are backed up. If `ha core check` fails, the bridge restores the previous files. A successful deployment records `last_known_good.json` for rollback.

## Request example

```json
{
  "id": "unique-request-id",
  "action": "deploy",
  "target_commit": "0123456789abcdef0123456789abcdef01234567",
  "scope": ["dashboards", "themes"]
}
```

The bridge does not execute shell commands supplied through Git. GitHub SSH host keys are pinned in the image and are not fetched at boot.

## Migration

0.5.0 is an in-place add-on upgrade because the slug remains `ha_grok_bridge`. The legacy `cloud_poll.py`, `command.json`, deploy shell workflow, and bundled `ha` shim are no longer runtime dependencies.
