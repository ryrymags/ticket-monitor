#!/bin/bash
# macOS setup script — run once after cloning the repo.
# Usage: bash deploy/setup_macos.sh
set -euo pipefail

echo "=== Ticket Monitor — macOS Setup ==="

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python is required. Install Python 3.11+ first."
  echo "Recommended: brew install python@3.12"
  exit 1
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
PY_OK="$("$PYTHON_BIN" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
if [ "$PY_OK" != "1" ]; then
  echo "Found Python $PY_VERSION, but this project requires >= 3.11."
  echo "Install and rerun, for example:"
  echo "  brew install python@3.12"
  echo "  PYTHON_BIN=/opt/homebrew/bin/python3.12 bash deploy/setup_macos.sh"
  exit 1
fi

REBUILD_VENV=0
if [ ! -x "venv/bin/python" ]; then
  REBUILD_VENV=1
else
  VENV_OK="$(venv/bin/python -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
  if [ "$VENV_OK" != "1" ]; then
    REBUILD_VENV=1
  fi
fi

if [ "$REBUILD_VENV" = "1" ]; then
  echo "Rebuilding virtual environment with Python $PY_VERSION..."
  rm -rf venv
  "$PYTHON_BIN" -m venv venv
fi

source venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python -m playwright install chromium

mkdir -p secrets logs scripts
chmod 700 secrets
chmod +x scripts/*.py 2>/dev/null || true
chmod +x scripts/monitorctl.sh 2>/dev/null || true
chmod +x scripts/install_desktop_shortcuts.sh 2>/dev/null || true
chmod +x scripts/browser_host.sh 2>/dev/null || true

if [ ! -f "config.yaml" ]; then
  echo "Missing config.yaml. Copy config.example.yaml and fill in webhook values."
  exit 1
fi

if grep -q "YOUR_WEBHOOK_URL_HERE" config.yaml; then
  echo "config.yaml still contains YOUR_WEBHOOK_URL_HERE."
  exit 1
fi

NEEDS_CHROME_CHANNEL="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
browser = raw.get("browser", {}) or {}
mode = str(browser.get("session_mode", "storage_state")).strip().lower()
channel = str(browser.get("channel", "")).strip().lower()
print("1" if mode == "persistent_profile" and channel == "chrome" else "0")
PY
)"
if [ "${NEEDS_CHROME_CHANNEL}" = "1" ]; then
  python -m playwright install chrome
fi

SESSION_MODE="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
mode = str(raw.get("browser", {}).get("session_mode", "storage_state")).strip().lower()
print(mode or "storage_state")
PY
)"

if [ "${SESSION_MODE}" = "persistent_profile" ]; then
  PROFILE_DIR="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
profile_dir = str(raw.get("browser", {}).get("user_data_dir", "secrets/tm_profile")).strip()
print(profile_dir or "secrets/tm_profile")
PY
)"
  if [ ! -d "${PROFILE_DIR}" ]; then
    echo "Missing ${PROFILE_DIR}. Run: python monitor.py --bootstrap-session"
    exit 1
  fi
elif [ "${SESSION_MODE}" = "cdp_attach" ]; then
  CHROME_PATH="$(python - <<'PY'
import os
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
browser_host = raw.get("browser_host", {}) or {}
path = str(
    browser_host.get(
        "chrome_executable_path",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
).strip()
print(path or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PY
)"
  HOST_PROFILE_DIR="$(python - <<'PY'
import os
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
browser_host = raw.get("browser_host", {}) or {}
profile_dir = str(browser_host.get("user_data_dir", "secrets/tm_chrome_profile")).strip() or "secrets/tm_chrome_profile"
if not os.path.isabs(profile_dir):
    profile_dir = os.path.normpath(os.path.join(os.getcwd(), profile_dir))
print(profile_dir)
PY
)"
  if [ ! -x "${CHROME_PATH}" ]; then
    echo "Missing Chrome executable: ${CHROME_PATH}"
    echo "Install Google Chrome and re-run setup."
    exit 1
  fi
  mkdir -p "${HOST_PROFILE_DIR}"
else
  if [ ! -f "secrets/tm_storage_state.json" ]; then
    echo "Missing secrets/tm_storage_state.json. Run: python monitor.py --bootstrap-session"
    exit 1
  fi
  chmod 600 secrets/tm_storage_state.json
fi

WATCHDOG_INTERVAL="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
value = raw.get("watchdog", {}).get("interval_seconds", 120)
try:
    value = int(value)
except (TypeError, ValueError):
    value = 120
print(max(10, value))
PY
)"

RELOADER_INTERVAL="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
value = raw.get("updates", {}).get("interval_seconds", 60)
try:
    value = int(value)
except (TypeError, ValueError):
    value = 60
print(max(10, value))
PY
)"

if [ "${SESSION_MODE}" != "cdp_attach" ]; then
  echo "Running doctor-lite checks..."
  python monitor.py --doctor-lite --config config.yaml
fi

PLIST_DIR="$HOME/Library/LaunchAgents"
MAIN_PLIST="$PLIST_DIR/com.ticketmonitor.plist"
GUARDIAN_PLIST="$PLIST_DIR/com.ticketmonitor.guardian.plist"
RELOADER_PLIST="$PLIST_DIR/com.ticketmonitor.reloader.plist"
BROWSER_HOST_PLIST="$PLIST_DIR/com.ticketmonitor.browser-host.plist"
mkdir -p "$PLIST_DIR"

cat > "$MAIN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ticketmonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>${REPO_DIR}/venv/bin/python</string>
    <string>${REPO_DIR}/monitor.py</string>
    <string>--config</string>
    <string>${REPO_DIR}/config.yaml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${REPO_DIR}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${REPO_DIR}/logs/launchd.err.log</string>
  <key>ProcessType</key>
  <string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
</dict>
</plist>
EOF

cat > "$GUARDIAN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ticketmonitor.guardian</string>
  <key>ProgramArguments</key>
  <array>
    <string>${REPO_DIR}/venv/bin/python</string>
    <string>${REPO_DIR}/scripts/guardian.py</string>
    <string>--config</string>
    <string>${REPO_DIR}/config.yaml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>${WATCHDOG_INTERVAL}</integer>
  <key>StandardOutPath</key>
  <string>${REPO_DIR}/logs/guardian.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${REPO_DIR}/logs/guardian.launchd.err.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

cat > "$RELOADER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ticketmonitor.reloader</string>
  <key>ProgramArguments</key>
  <array>
    <string>${REPO_DIR}/venv/bin/python</string>
    <string>${REPO_DIR}/scripts/reloader.py</string>
    <string>--config</string>
    <string>${REPO_DIR}/config.yaml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>${RELOADER_INTERVAL}</integer>
  <key>StandardOutPath</key>
  <string>${REPO_DIR}/logs/reloader.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${REPO_DIR}/logs/reloader.launchd.err.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

BROWSER_HOST_ENABLED="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
browser = raw.get("browser", {}) or {}
mode = str(browser.get("session_mode", "storage_state")).strip().lower()
host = raw.get("browser_host", {}) or {}
enabled = bool(host.get("enabled", mode == "cdp_attach"))
print("1" if mode == "cdp_attach" and enabled else "0")
PY
)"
if [ "${BROWSER_HOST_ENABLED}" = "1" ]; then
  BROWSER_HOST_CHROME_PATH="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
host = raw.get("browser_host", {}) or {}
path = str(
    host.get(
        "chrome_executable_path",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
).strip()
print(path or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PY
)"
  BROWSER_HOST_PROFILE_DIR="$(python - <<'PY'
import os
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
host = raw.get("browser_host", {}) or {}
profile = str(host.get("user_data_dir", "secrets/tm_chrome_profile")).strip() or "secrets/tm_chrome_profile"
if not os.path.isabs(profile):
    profile = os.path.normpath(os.path.join(os.getcwd(), profile))
print(profile)
PY
)"
  BROWSER_HOST_PORT="$(python - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
host = raw.get("browser_host", {}) or {}
port = host.get("remote_debugging_port", 9222)
try:
    port = int(port)
except (TypeError, ValueError):
    port = 9222
print(max(1, port))
PY
)"
  mkdir -p "${BROWSER_HOST_PROFILE_DIR}"
  cat > "$BROWSER_HOST_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ticketmonitor.browser-host</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BROWSER_HOST_CHROME_PATH}</string>
    <string>--remote-debugging-port=${BROWSER_HOST_PORT}</string>
    <string>--user-data-dir=${BROWSER_HOST_PROFILE_DIR}</string>
    <string>--no-first-run</string>
    <string>--no-default-browser-check</string>
    <string>about:blank</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${REPO_DIR}/logs/browser-host.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${REPO_DIR}/logs/browser-host.launchd.err.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF
fi

launchctl unload "$MAIN_PLIST" >/dev/null 2>&1 || true
launchctl unload "$GUARDIAN_PLIST" >/dev/null 2>&1 || true
launchctl unload "$RELOADER_PLIST" >/dev/null 2>&1 || true
launchctl unload "$BROWSER_HOST_PLIST" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)/com.ticketmonitor.browser-host" >/dev/null 2>&1 || true

if [ "${BROWSER_HOST_ENABLED}" = "1" ]; then
  launchctl load "$BROWSER_HOST_PLIST"
fi

launchctl load "$MAIN_PLIST"
launchctl load "$GUARDIAN_PLIST"
launchctl load "$RELOADER_PLIST"

if [ "${BROWSER_HOST_ENABLED}" = "1" ]; then
  launchctl kickstart -k "gui/$(id -u)/com.ticketmonitor.browser-host" || true
  "${REPO_DIR}/scripts/browser_host.sh" ensure --config "${REPO_DIR}/config.yaml"
fi
launchctl kickstart -k "gui/$(id -u)/com.ticketmonitor" || true
launchctl kickstart -k "gui/$(id -u)/com.ticketmonitor.guardian" || true
launchctl kickstart -k "gui/$(id -u)/com.ticketmonitor.reloader" || true

if [ "${SESSION_MODE}" = "cdp_attach" ]; then
  echo "Running doctor-lite checks..."
  python monitor.py --doctor-lite --config config.yaml
fi

"${REPO_DIR}/scripts/install_desktop_shortcuts.sh"

deactivate

echo
echo "=== Setup complete ==="
echo "LaunchAgents:"
echo "  $MAIN_PLIST"
echo "  $GUARDIAN_PLIST"
echo "  $RELOADER_PLIST"
if [ "${BROWSER_HOST_ENABLED}" = "1" ]; then
  echo "  $BROWSER_HOST_PLIST"
fi
echo
echo "Power settings to prevent interruption (run once):"
echo "  sudo pmset -a sleep 0 disksleep 0 displaysleep 10"
echo "  sudo pmset -a tcpkeepalive 1 powernap 0"
echo
echo "Friendly controls:"
echo "  ${REPO_DIR}/scripts/monitorctl.sh status"
echo "  ${REPO_DIR}/scripts/monitorctl.sh verify"
echo "  ${REPO_DIR}/scripts/monitorctl.sh verify-webhook"
echo "  ${REPO_DIR}/scripts/monitorctl.sh fix"
echo "  ${REPO_DIR}/scripts/monitorctl.sh doctor"
echo "  ${REPO_DIR}/scripts/monitorctl.sh reauth"
