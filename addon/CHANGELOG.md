# Changelog

## 0.5.0 — 2026-09-01

- Replaced the legacy command poller with structured Git file synchronization.
- Added snapshot, validate, deploy, rollback, and status actions.
- Deploys require a full commit SHA that is an ancestor of `main`.
- Added explicit path/scope allow-list and runtime/secrets exclusions.
- Added local backups, automatic restore after failed `ha core check`, and last-known-good tracking.
- Pinned GitHub SSH host keys; removed boot-time `ssh-keyscan`.
- Kept the existing add-on slug `ha_grok_bridge` for in-place upgrade.

## 0.2.4 — 2026-09-01

- homeassistant_config explizit path: /config (sonst Mount /homeassistant, Start-Crash).
- init: true, damit with-contenv/bashio existiert.

## 0.2.3 — 2026-09-01

- First boot: aktuelle command.json-id nur armen, nicht ausführen.
- result.json: pull --rebase, Push-Exit loggen.
- boot: manual. backup_exclude für Keys und Clone.
