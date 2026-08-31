#!/bin/bash
set -euo pipefail
mkdir -p /config/dashboards /config/themes /config/www
git -C /config pull origin main
[ -f /config/zuhause.yaml ] && cp -f /config/zuhause.yaml /config/dashboards/zuhause.yaml
[ -f /config/timo.yaml ] && cp -f /config/timo.yaml /config/dashboards/timo.yaml
[ -f /config/apple.yaml ] && cp -f /config/apple.yaml /config/themes/apple.yaml
[ -f /config/apple-optik.js ] && cp -f /config/apple-optik.js /config/www/apple-optik.js
test -f /config/dashboards/zuhause.yaml
test -f /config/dashboards/timo.yaml
ha core check
ha core reload
