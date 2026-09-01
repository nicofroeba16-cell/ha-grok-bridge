#!/bin/bash
# Welle 0+1 in einer Datei. Host entscheidet.
# HA-Box  → 0: Key pruefen, addon/ nach /addons, kein Start
# ki2     → 1: Poller stop+disable, Add-on nicht starten
# Erst ausfuehren nach Freigabe. Nie beide Poller parallel an.
set -euo pipefail

PUB='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBTGGCumX9DTtxZNlomqhcwaz0m6v+M10oqA0sHNtcip ha-grok-bridge'
REPO='https://github.com/nicofroeba16-cell/ha-grok-bridge'
KEY=/ssl/ha-grok-bridge

is_ki2() { [[ -d /home/vboxuser/grok-agent && ! -d /addons ]]; }
is_ha() { [[ -d /addons || -d /usr/share/hassio/addons ]]; }

welle0_ha() {
  local addons dest tmp
  addons=/addons
  [[ -d /addons ]] || addons=/usr/share/hassio/addons
  dest="$addons/ha_grok_bridge"
  mkdir -p /ssl "$dest"
  if [[ ! -f "$KEY" ]]; then
    echo "ABBRUCH: $KEY fehlt. ha-grok-bridge.key nach $KEY, chmod 600."
    echo "Pub fuer GitHub Deploy key / Allow write:"
    echo "$PUB"
    exit 1
  fi
  chmod 600 "$KEY"
  tmp=$(mktemp -d)
  git clone --depth 1 "$REPO" "$tmp/src"
  cp -a "$tmp/src/addon/." "$dest/"
  rm -rf "$tmp"
  chmod a+x "$dest/run.sh" "$dest/ha" "$dest/wave-01.sh" || true
  grep -q 'version: "0.2.4"' "$dest/config.yaml" || {
    echo "ABBRUCH: nicht 0.2.4"
    exit 1
  }
  echo "WELLE0 OK $dest + $KEY"
  echo "WELLE0 Pub: $PUB"
  echo "UI: Add-ons neu laden, Bauen, Start AUS bis ki2 stop."
}

welle1_ki2() {
  echo "WELLE1 stop ha-grok-bridge.service"
  systemctl stop ha-grok-bridge.service
  systemctl disable ha-grok-bridge.service
  echo -n "WELLE1 active="
  systemctl is-active ha-grok-bridge.service || true
  echo
  echo "SOFORT HA: Add-on Start. Log: first boot arm skip exec"
  echo "Rollback: systemctl enable --now ha-grok-bridge.service"
}

if is_ki2; then
  welle1_ki2
elif is_ha; then
  welle0_ha
else
  echo "ABBRUCH: weder ki2 noch HA-Box."
  exit 1
fi
