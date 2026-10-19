#!/bin/bash
# Installs the recorder as a systemd service on Linux. Run by `make install` with sudo.
# Usage: sudo UV=/path/to/uv scripts/install-linux.sh <repo dir> <config.json> <user>
set -euo pipefail
REPO="$1"; CONFIG="$2"; RUN_AS="$3"
APP="elegoo-print-recorder"
PREFIX="/opt/$APP"                     # root-owned app; the service itself runs as $RUN_AS
UNIT="/etc/systemd/system/$APP.service"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
STOPPED=no
# On any failure: say where, and never leave the recorder stopped.
trap 'echo "  ✗ install failed at scripts/install-linux.sh line $LINENO: $BASH_COMMAND"; [ "$STOPPED" = yes ] && systemctl start "$APP" 2>/dev/null && echo "  (recorder started again with the previous setup)"' ERR

# 1. Build the new app next to the old one; the running service is untouched until this works.
export UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_CACHE_DIR="$PREFIX/cache" UV_NO_CONFIG=1
mkdir -p "$PREFIX"
rm -rf "$PREFIX/venv.new"
"$UV" venv --quiet --relocatable --managed-python --python 3.12 "$PREFIX/venv.new"
"$UV" pip install --quiet --python "$PREFIX/venv.new/bin/python" "$REPO"
"$PREFIX/venv.new/bin/$APP" version >/dev/null
rm -rf "$PREFIX/cache"

# 2. Swap it in. Stopping the old service lets it finish any recording cleanly.
systemctl stop "$APP" 2>/dev/null || true
STOPPED=yes
rm -rf "$PREFIX/venv.old"
if [ -d "$PREFIX/venv" ]; then mv "$PREFIX/venv" "$PREFIX/venv.old"; fi
mv "$PREFIX/venv.new" "$PREFIX/venv"
rm -rf "$PREFIX/venv.old"
chown -R root:root "$PREFIX"; chmod -R go-w "$PREFIX"

install -d -m 0755 "/etc/$APP"
[ "$CONFIG" -ef "/etc/$APP/config.json" ] || install -m 0644 "$CONFIG" "/etc/$APP/config.json"
# No hard dependency on the recordings folder's disk (e.g. a NAS mount): at boot the
# network may not be up yet, and systemd would then skip the service entirely.
# The recorder records to local disk and retries the copy until the NAS is back.

cat > "$UNIT" <<UNIT
[Unit]
Description=Elegoo print recorder (records the printer camera for every print)
Wants=network-online.target
After=network-online.target remote-fs.target tailscaled.service

[Service]
User=$RUN_AS
ExecStart=$PREFIX/venv/bin/$APP run
Restart=always
RestartSec=10
# Only the recorder gets SIGTERM; it stops ffmpeg gently so the camera slot is freed.
KillMode=mixed
TimeoutStopSec=300
StateDirectory=$APP
LogsDirectory=$APP

[Install]
WantedBy=multi-user.target
UNIT

# 3. Start it now and at every boot, then wait for a fresh "watching printer" line.
systemctl daemon-reload
since=$(date '+%Y-%m-%d %H:%M:%S')
systemctl enable --now "$APP" >/dev/null 2>&1
systemctl restart "$APP"
for _ in $(seq 1 30); do
  if journalctl -u "$APP" --since "$since" --no-pager 2>/dev/null | grep -q "watching printer"; then break; fi
  sleep 0.5
done
if systemctl is-active --quiet "$APP" && systemctl is-enabled --quiet "$APP" \
   && journalctl -u "$APP" --since "$since" --no-pager | grep -q "watching printer"; then
  echo "  ✓ recorder running as a service (starts at every boot, restarts if it crashes)"
else
  echo "  ✗ the recorder did not start; see: journalctl -u $APP"; exit 1
fi
