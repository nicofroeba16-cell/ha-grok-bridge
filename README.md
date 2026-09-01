# HA File Sync Bridge

Home Assistant add-on for controlled bidirectional synchronization of `/config` with a Git repository.

## Funktionen

- Home Assistant `/config` -> GitHub
- GitHub -> Home Assistant `/config`
- bidirektionaler Synchronisationsmodus
- Konflikterkennung bei Änderungen auf beiden Seiten
- lokale, zeitgestempelte Snapshots
- automatische Snapshot-Begrenzung und Bereinigung
- Snapshot-Restore
- Dry-Run-Modus
- konfigurierbare Ausschlusslisten
- Secret-Scan vor jedem Push
- Schutz von SSH-, Lock-, Datenbank-, Log- und Laufzeitdateien
- Initial-Sync HA -> GitHub oder GitHub -> HA
- manuelle Synchronisation über Add-on-Webinterface/Ingress
- Status-/Health-Datei unter `/data/status.json`
- Status-Endpunkt `/status`
- Snapshot-Liste `/snapshots`
- Restore über `/restore/<snapshot>`
- History-Cleanup für versehentlich übertragene Secret-Pfade, nur bei explizit aktivierter Option
- Lock gegen parallele Synchronisationen
- saubere Behandlung gelöschter Dateien

## Webinterface-Endpunkte

- `GET /status`
- `GET /snapshots`
- `POST /sync/up`
- `POST /sync/down`
- `POST /restore/<snapshot>`
- `POST /history-cleanup`

## Sicherheit

Private Schlüssel, Passphrasen, `secrets.yaml`, `.storage`, `.ssh`, Datenbanken, Logs, Locks und weitere Laufzeitdaten werden standardmäßig nicht synchronisiert. Der Secret-Scan blockiert einen Push, wenn verdächtige Geheimnisse erkannt werden.

## Installation

Repository: https://github.com/nicofroeba16-cell/ha-grok-bridge

Zielrepository für die Konfiguration: https://github.com/nicofroeba16-cell/ha-grok-bridge-live
