# Integration 0.2.4 — sinnvollste Reihenfolge

Nicht eingespielt. ki2 bleibt an, bis Welle 2.

Pub:
`ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBTGGCumX9DTtxZNlomqhcwaz0m6v+M10oqA0sHNtcip ha-grok-bridge`

## Welle 0 — parallel, ki2 unberührt

**0a GitHub** (kein Box-Zugriff)
https://github.com/nicofroeba16-cell/ha-grok-bridge/settings/keys
Add deploy key `ha-addon` · Pub oben · Allow write.

**0b HA-Box** (SSH-Add-on, nicht ki2)
```
cp ha-grok-bridge.key /ssl/ha-grok-bridge && chmod 600 /ssl/ha-grok-bridge
mkdir -p /addons/ha_grok_bridge
git clone --depth 1 https://github.com/nicofroeba16-cell/ha-grok-bridge /tmp/ha-grok-bridge
cp -a /tmp/ha-grok-bridge/addon/. /addons/ha_grok_bridge/
```
UI: Add-ons neu laden → Bauen → Start **aus**.
Gate 0: Image 0.2.4. ki2 weiter `active`.

## Welle 1 — Schnitt

Nur nach **OK ki2 stop**:
```
systemctl stop ha-grok-bridge.service
systemctl disable ha-grok-bridge.service
```
Sofort Add-on Start.
Gate 1: `first boot: arm last_id=… skip exec`

## Welle 2 — Beweis

Nach **OK Canary**:
`{"id":"ha-20260901-030000-inf0","command":"ha core info"}`
Gate 2: `result.json` `via=addon-0.2.4` `ok=true`
Ohne Gate 2: Add-on stop, ki2 wieder an.

## Welle 3 — Arbeit

Nach **OK Deploy**: Standard-`deploy.sh`.
Danach optional `boot: auto`. ki2 7 Tage behalten.
