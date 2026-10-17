#!/bin/bash
# Installs the recorder as a system background service. Run by `make install` with sudo.
# Usage: sudo UV=/path/to/uv scripts/install-service.sh <repo dir> <config.json>
set -euo pipefail
REPO="$1"; CONFIG="$2"
LABEL="io.github.shreyashguptas.elegoo-print-recorder"
PREFIX="/usr/local/lib/elegoo-print-recorder"     # root-owned, so the service can't be tampered with
PLIST="/Library/LaunchDaemons/$LABEL.plist"
SUPPORT="/Library/Application Support/elegoo-print-recorder"
LOGS="/Library/Logs/elegoo-print-recorder"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

# 1. Build the new app next to the old one; the running service is untouched until this works.
export UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_CACHE_DIR="$PREFIX/cache" UV_NO_CONFIG=1
mkdir -p "$PREFIX"
rm -rf "$PREFIX/venv.new"
"$UV" venv --quiet --relocatable --managed-python --python 3.12 "$PREFIX/venv.new"
"$UV" pip install --quiet --python "$PREFIX/venv.new/bin/python" "$REPO"
"$PREFIX/venv.new/bin/elegoo-print-recorder" version >/dev/null
rm -rf "$PREFIX/cache"

# 2. Swap it in. Stopping the old service lets it finish any recording cleanly.
launchctl bootout "system/$LABEL" 2>/dev/null || true
rm -rf "$PREFIX/venv.old"
if [ -d "$PREFIX/venv" ]; then mv "$PREFIX/venv" "$PREFIX/venv.old"; fi
mv "$PREFIX/venv.new" "$PREFIX/venv"
rm -rf "$PREFIX/venv.old"
chown -R root:wheel "$PREFIX"; chmod -R go-w "$PREFIX"

mkdir -p "$SUPPORT" "$LOGS"
install -m 0644 "$CONFIG" "$SUPPORT/config.json"

# 3. The camera sends video over UDP; let the service's programs through the firewall.
FFMPEG=$("$PREFIX/venv/bin/python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["ffmpeg"])' "$CONFIG")
"$HERE/allow-firewall.sh" "$FFMPEG" "$(dirname "$FFMPEG")/ffprobe" "$PREFIX/venv/bin/python" >/dev/null || true

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PREFIX/venv/bin/elegoo-print-recorder</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>300</integer>
  <key>StandardOutPath</key><string>$LOGS/service.log</string>
  <key>StandardErrorPath</key><string>$LOGS/service.log</string>
</dict>
</plist>
PLIST
chmod 644 "$PLIST"; chown root:wheel "$PLIST"

# 4. Start it and wait for a fresh "watching printer" line.
before=$(cat "$LOGS/recorder.log" 2>/dev/null | wc -l | tr -d ' ')
launchctl bootstrap system "$PLIST"
launchctl enable "system/$LABEL"
started=no
for _ in $(seq 1 30); do
  if tail -n +"$((before + 1))" "$LOGS/recorder.log" 2>/dev/null | grep -q "watching printer"; then started=yes; break; fi
  sleep 0.5
done
if [ "$started" = yes ] && launchctl print "system/$LABEL" 2>/dev/null | grep -q "state = running"; then
  echo "  ✓ background recorder running (starts automatically with your Mac)"
else
  echo "  ✗ the background recorder did not start; see $LOGS/service.log"; exit 1
fi
