#!/bin/bash
# One-time setup for the guardian's last-resort self-heal reboot (FileVault
# authenticated restart). Stores your macOS login credentials root-only at
# /etc/ticketmonitor/authrestart.plist (never leaves the encrypted disk) and adds a
# NOPASSWD sudoers entry for exactly the authrestart command.
#
# Run:  sudo bash scripts/setup_selfheal_reboot.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash $0"
  exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-}}"
if [ -z "${TARGET_USER}" ]; then
  echo "Usage: sudo bash $0 [username]"
  exit 1
fi

if ! fdesetup supportsauthrestart 2>/dev/null | grep -qi true; then
  echo "This Mac does not support FileVault authenticated restart; aborting."
  exit 1
fi

read -r -s -p "macOS login password for ${TARGET_USER}: " USER_PW
echo

# Verify the password before storing it — a wrong one would strand every reboot.
if ! dscl . -authonly "${TARGET_USER}" "${USER_PW}" >/dev/null 2>&1; then
  echo "Password check failed for ${TARGET_USER}; nothing written."
  exit 1
fi

mkdir -p /etc/ticketmonitor
TARGET_USER="${TARGET_USER}" USER_PW="${USER_PW}" /usr/bin/python3 - <<'PY'
import os
import plistlib

with open("/etc/ticketmonitor/authrestart.plist", "wb") as f:
    plistlib.dump(
        {"Username": os.environ["TARGET_USER"], "Password": os.environ["USER_PW"]}, f
    )
PY
chown root:wheel /etc/ticketmonitor/authrestart.plist
chmod 600 /etc/ticketmonitor/authrestart.plist

echo "${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/fdesetup authrestart -inputplist" \
  > /etc/sudoers.d/ticketmonitor
chmod 440 /etc/sudoers.d/ticketmonitor
visudo -c >/dev/null

echo "OK: self-heal reboot is armed."
echo "  Credentials: /etc/ticketmonitor/authrestart.plist (root-only, 600)"
echo "  Sudoers:     /etc/sudoers.d/ticketmonitor (authrestart only)"
echo "Test any time with: sudo -n /usr/bin/fdesetup authrestart -inputplist < /etc/ticketmonitor/authrestart.plist"
echo "(That command WILL reboot the Mac.)"
