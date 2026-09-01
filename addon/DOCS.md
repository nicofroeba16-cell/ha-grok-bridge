# HA Grok Bridge 0.2.3

Lokales Add-on. **ki2 erst stoppen, wenn Canary grün.**

Nur Git. Keine HTTP-API, kein Port.

## Cutover (Reihenfolge hart)

1. Key: `/ssl/ha-grok-bridge` (ed25519), Deploy-Key **nur** Repo `ha-grok-bridge` (write).
2. Add-on nach `/addons/ha_grok_bridge` kopieren, Supervisor neu laden, lokal bauen. **Nicht starten**, solange ki2 läuft.
3. ki2: `systemctl stop ha-grok-bridge.service && systemctl disable ha-grok-bridge.service`
4. Add-on starten. First boot **armt** aktuelle `command.json`-id, führt sie nicht aus.
5. Neue id, nur `ha core info`.
6. GitHub `result.json` muss `"via": "addon-0.2.3"` und `"ok": true` haben.
7. Erst dann Deploy-Kette. Ohne Schritt 6: Add-on stop, ki2 `systemctl start`.
