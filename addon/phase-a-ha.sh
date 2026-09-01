#!/bin/bash
# Phase A nur auf der HA-Box (SSH-Add-on / Host-root). Nicht auf ki2.
set -euo pipefail

if [[ -d /home/vboxuser/grok-agent && ! -d /addons ]]; then
  echo "ABBRUCH: das ist ki2. Script auf der HA-Box ausfuehren."
  exit 1
fi
if [[ ! -d /addons && ! -d /usr/share/hassio/addons ]]; then
  echo "ABBRUCH: kein /addons. Falscher Host."
  exit 1
fi

ADDONS=/addons
if [[ ! -d /addons && -d /usr/share/hassio/addons ]]; then
  ADDONS=/usr/share/hassio/addons
fi
DEST="$ADDONS/ha_grok_bridge"
KEY=/ssl/ha-grok-bridge
REPO="https://github.com/nicofroeba16-cell/ha-grok-bridge"

mkdir -p /ssl "$DEST"

if [[ ! -f "$KEY" ]]; then
  ssh-keygen -t ed25519 -f "$KEY" -N "" -C ha-grok-bridge
  echo "KEY neu: $KEY"
else
  echo "KEY existiert: $KEY"
fi
chmod 600 "$KEY"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if command -v git >/dev/null 2>&1; then
  git clone --depth 1 "$REPO" "$TMP/src"
else
  echo "ABBRUCH: git fehlt. Zip nach $DEST entpacken."
  exit 1
fi

SRC="$TMP/src/addon"
if [[ ! -f "$SRC/config.yaml" ]]; then
  echo "ABBRUCH: addon/config.yaml nicht im Repo-Clone."
  exit 1
fi
cp -a "$SRC/." "$DEST/"
chmod a+x "$DEST/run.sh" "$DEST/ha" || true

echo
echo "=== Phase A Dateien ==="
ls -l "$DEST/config.yaml" "$DEST/cloud_poll.py" "$DEST/Dockerfile" "$DEST/run.sh" "$DEST/ha"
echo
echo "=== Public Key — GitHub ha-grok-bridge Deploy key, Allow write ==="
cat "${KEY}.pub"
echo
echo "Danach: Supervisor Add-ons neu laden, HA Grok Bridge BAUEN, nicht starten."
echo "ki2 nicht anfassen."
