# Cutover 0.2.4 — vorbereitet, nicht eingespielt

Live bleibt ki2. `command.json` auf GitHub nicht anfassen, bis Freigabe „einspielen“.

Public Key:
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBTGGCumX9DTtxZNlomqhcwaz0m6v+M10oqA0sHNtcip ha-grok-bridge
```

## 1 Key auf HA-Box
```
cp ha-grok-bridge.key /ssl/ha-grok-bridge
chmod 600 /ssl/ha-grok-bridge
```

## 2 Deploy key
https://github.com/nicofroeba16-cell/ha-grok-bridge/settings/keys
Title `ha-addon` · Zeile oben · Allow write

## 3 Ordner 0.2.4
```
mkdir -p /addons/ha_grok_bridge
git clone --depth 1 https://github.com/nicofroeba16-cell/ha-grok-bridge /tmp/ha-grok-bridge
cp -a /tmp/ha-grok-bridge/addon/. /addons/ha_grok_bridge/
```

## 4 Bauen
Add-ons neu laden → HA Grok Bridge → Bauen. Start aus.
Log: `0.2.4` und `/config ok`

## 5 ki2 stop (nur nach extra OK)
```
systemctl stop ha-grok-bridge.service
systemctl disable ha-grok-bridge.service
```

## 6 Add-on start
UI Start. Log: `first boot: arm last_id=… skip exec`

## 7 Canary (Agent schreibt command.json erst nach Freigabe)
```
{"id":"ha-20260901-030000-inf0","command":"ha core info"}
```
Gate: GitHub result.json `"via":"addon-0.2.4"` `"ok":true`

## 8 Deploy (eigene Freigabe)
```
git -C /config fetch origin main && git -C /config checkout origin/main -- deploy.sh && bash /config/deploy.sh
```
