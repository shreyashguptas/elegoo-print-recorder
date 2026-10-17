#!/bin/bash
# Removes the background service. Your recordings are kept.
set -euo pipefail
LABEL="io.github.shreyashguptas.elegoo-print-recorder"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
launchctl bootout "system/$LABEL" 2>/dev/null || true
rm -f "/Library/LaunchDaemons/$LABEL.plist"
rm -rf /usr/local/lib/elegoo-print-recorder "/Library/Application Support/elegoo-print-recorder" /Library/Logs/elegoo-print-recorder
echo "  ✓ removed (recordings were kept)"
