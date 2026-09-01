# HA File Sync Bridge

Home Assistant add-on for controlled bidirectional synchronization of `/config` with a Git repository.

## Funktionen

- HA `/config` -> GitHub
- GitHub -> HA `/config`
- bidirektionaler Sync
- Konflikterkennung bei Änderungen auf beiden Seiten
- zeitgestempelte lokale Snapshots
- Snapshot-Limit und automatische Bereinigung
- Snapshot-Restore mit Sicherheits-Snapshot davor
- Dry-Run
- konfigurierbare Ausschlusslisten
- Secret-Scan vor Push
- Schutz für SSH-, Lock-, Datenbank-, Log- und Laufzeitdateien
- Initial-Sync in beide Richtungen
- manuelle Sync-/Restore-Aktionen über Add-on Ingress
- Health-/Statusdatei `/data/status.json`
- Lock gegen parallele Syncs
- saubere Erkennung gelöschter Dateien
- opt-in History-Cleanup für versehentlich übertragene Secret-Pfade

## Manuelle API

`GET /status`

`GET /snapshots`

`POST /sync/up`

`POST /sync/down`

`POST /restore/<snapshot>`

`POST /history-cleanup`

## Sicherheit

Private Schlüssel, Passphrasen, `secrets.yaml`, `.storage`, `.ssh`, `.cache`, Datenbanken, Logs, Locks und weitere Laufzeitdaten werden standardmäßig ausgeschlossen. Ein Secret-Scan blockiert verdächtige Inhalte vor dem Commit/Push.

## Installation

Add-on-Repository: https://github.com/nicofroeba16-cell/ha-grok-bridge

Konfigurationsziel: https://github.com/nicofroeba16-cell/ha-grok-bridge-live
