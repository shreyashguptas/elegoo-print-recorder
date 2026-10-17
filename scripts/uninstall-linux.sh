#!/bin/bash
# Removes the Linux service. Your recordings are kept.
set -euo pipefail
APP="elegoo-print-recorder"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
systemctl disable --now "$APP" 2>/dev/null || true
rm -f "/etc/systemd/system/$APP.service"; systemctl daemon-reload
rm -rf "/opt/$APP" "/etc/$APP" "/var/lib/$APP" "/var/log/$APP"
echo "  ✓ removed (recordings were kept)"
