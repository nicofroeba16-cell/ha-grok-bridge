#!/bin/bash
# Welle 0b nur HA-Box. Kein Start, kein ki2-Stop.
set -euo pipefail

if [[ -d /home/vboxuser/grok-agent && ! -d /addons ]]; then
  echo "ABBRUCH: ki2. Auf der HA-Box ausfuehren."
  exit 1
fi
if [[ ! -d /addons && ! -d /usr/share/hassio/addons ]]; then
  echo "ABBRUCH: kein /addons."
  exit 1
fi

ADDONS=/addons
[[ -d /addons ]] || ADDONS=/usr/share/hassio/addons
DEST="$ADDONS/ha_grok_bridge"
KEY=/ssl/ha-grok-bridge
REPO="https://github.com/nicofroeba16-cell/ha-grok-bridge"
PUB='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBTGGCumX9DTtxZNlomqhcwaz0m6v+M10oqA0sHNtcip ha-grok-bridge'

mkdir -p /ssl "$DEST"

if [[ ! -f "$KEY" ]]; then
  echo "ABBRUCH: $KEY fehlt."
  echo "Private Datei ha-grok-bridge.key nach $KEY kopieren, chmod 600."
  echo "Kein ssh-keygen — sonst passt der GitHub-Deploy-Key nicht."
  exit 1
fi
chmod 600 "$KEY"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git clone --depth 1 "$REPO" "$TMP/src"
cp -a "$TMP/src/addon/." "$DEST/"
chmod a+x "$DEST/run.sh" "$DEST/ha" "$DEST/wave-0-ha.sh" || true

grep -q 'version: "0.2.4"' "$DEST/config.yaml" || {
  echo "ABBRUCH: config.yaml nicht 0.2.4"
  exit 1
}

echo "WELLE0 Dateien OK in $DEST"
echo "WELLE0 Key OK $KEY"
echo "WELLE0 Pub (GitHub Deploy key, Allow write):"
echo "$PUB"
echo "Danach UI: Add-ons neu laden, Bauen, Start aus. ki2 nicht anfassen."
