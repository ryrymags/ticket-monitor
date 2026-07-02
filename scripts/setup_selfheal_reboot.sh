#!/bin/bash
# One-time setup for the guardian's last-resort self-heal reboot.
#
# Requires FileVault OFF + macOS automatic login configured for your account first
# (System Settings > Privacy & Security > FileVault > Turn Off; then
# System Settings > Users & Groups > Login Options > Automatically log in as you).
# With FileVault on, `fdesetup authrestart` unlocks the disk silently but still
# leaves a login-window flash requiring a password on some macOS versions — verified
# not to be zero-touch here, so it isn't used. Your account password is untouched by
# any of this; only the boot-time login screen is skipped.
#
# This just grants a NOPASSWD sudo rule for one fixed reboot command — no
# credentials are stored anywhere.
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

if fdesetup status | grep -qi "filevault is on"; then
  echo "FileVault is still on. Turn it off first (System Settings > Privacy &"
  echo "Security > FileVault) and enable automatic login (Users & Groups > Login"
  echo "Options), then re-run this script."
  exit 1
fi

if ! defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser >/dev/null 2>&1; then
  echo "Automatic login is not configured. Enable it in System Settings > Users &"
  echo "Groups > Login Options > Automatically log in as ${TARGET_USER}, then re-run."
  exit 1
fi

# Clean up the old FileVault-authrestart approach if a previous run set it up —
# that plaintext-password plist is unnecessary now that FileVault is off.
rm -f /etc/ticketmonitor/authrestart.plist /etc/ticketmonitor/trigger_authrestart.sh

echo "${TARGET_USER} ALL=(root) NOPASSWD: /sbin/shutdown -r now" \
  > /etc/sudoers.d/ticketmonitor
chmod 440 /etc/sudoers.d/ticketmonitor
visudo -c >/dev/null

echo "OK: self-heal reboot is armed."
echo "  Sudoers: /etc/sudoers.d/ticketmonitor (only 'shutdown -r now', nothing else)"
echo "Test any time with: sudo -n /sbin/shutdown -r now"
echo "(That command WILL reboot the Mac. It should land back at your desktop with no input.)"
