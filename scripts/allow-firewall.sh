#!/bin/bash
# Lets the given programs receive the printer's replies and camera video through
# the macOS firewall (only matters when the firewall is on). Run with sudo.
set -euo pipefail
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
"$FW" --getglobalstate | grep -qi enabled || exit 0
for bin in "$@"; do
  real=$(/bin/realpath "$bin" 2>/dev/null || echo "$bin")
  [ -x "$real" ] || continue
  "$FW" --add "$real" >/dev/null
  "$FW" --unblockapp "$real" >/dev/null
done
echo "  ✓ firewall allows the recorder to hear the printer"
