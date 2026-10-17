#!/bin/bash
# Mounts an SMB share from a NAS so recordings can be saved there. Run by `make nas` with sudo.
# Usage: sudo scripts/mount-nas-linux.sh //NAS-IP/share nas-user local-user
# Asks for the NAS password itself and stores it in a root-only file.
set -euo pipefail
SHARE="$1"; NAS_USER="$2"; RUN_AS="$3"
NAME="$(basename "$SHARE")"
MNT="/mnt/$NAME"
CRED="/etc/elegoo-print-recorder/nas-$NAME.cred"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
command -v mount.cifs >/dev/null || { echo "Installing cifs-utils..."; apt-get install -y -qq cifs-utils >/dev/null; }

install -d -m 0755 /etc/elegoo-print-recorder "$MNT"
if [ ! -s "$CRED" ]; then
  read -r -s -p "Password for NAS user $NAS_USER: " PASS; echo
  (umask 077; printf 'username=%s\npassword=%s\n' "$NAS_USER" "$PASS" > "$CRED")
  unset PASS
fi
chmod 600 "$CRED"

uid=$(id -u "$RUN_AS"); gid=$(id -g "$RUN_AS")
OPTS="credentials=$CRED,uid=$uid,gid=$gid,file_mode=0644,dir_mode=0755,vers=3.0,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30,x-systemd.after=network-online.target,x-systemd.after=tailscaled.service"
LINE="$SHARE $MNT cifs $OPTS 0 0"
grep -v " $MNT cifs " /etc/fstab > /etc/fstab.new || true
echo "$LINE" >> /etc/fstab.new
chmod 644 /etc/fstab.new; mv /etc/fstab.new /etc/fstab
systemctl daemon-reload
systemctl restart "$(systemd-escape -p --suffix=automount "$MNT")" 2>/dev/null || true

# Prove it works: write, read back, delete.
probe="$MNT/.write-test-$(hostname)"
if sudo -u "$RUN_AS" sh -c "echo ok > '$probe' && cat '$probe' >/dev/null && rm '$probe'"; then
  echo "  ✓ NAS share mounted at $MNT (comes back by itself after a restart)"
else
  echo "  ✗ could not write to $MNT. Check the NAS user name, password and share permissions."; exit 1
fi
