#!/bin/bash
# Oracle VM setup script — run once after cloning the repo.
# Usage: bash deploy/setup.sh
set -e

echo "=== Ticket Monitor — VM Setup ==="

# Install Python if missing
if ! command -v python3 &>/dev/null; then
    echo "Installing Python 3..."
    sudo apt update && sudo apt install -y python3 python3-pip python3-venv
fi

# Create venv and install deps
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r requirements.txt
python -m playwright install chromium
echo "Dependencies installed."

# Ensure secrets directory exists with restrictive permissions
mkdir -p secrets
chmod 700 secrets
chmod +x scripts/monitorctl.sh scripts/install_desktop_shortcuts.sh scripts/browser_host.sh 2>/dev/null || true

# Prompt for config if still using placeholders
if grep -q "YOUR_WEBHOOK_URL_HERE" config.yaml 2>/dev/null; then
    echo ""
    echo ">>> config.yaml still has placeholder values."
    echo ">>> Edit it now:  nano config.yaml"
    echo ">>> Then re-run this script."
    exit 1
fi

NEEDS_CHROME_CHANNEL="$(python3 - <<'PY'
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

SESSION_MODE="$(python3 - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
mode = str(raw.get("browser", {}).get("session_mode", "storage_state")).strip().lower()
print(mode or "storage_state")
PY
)"

if [ "${SESSION_MODE}" = "persistent_profile" ]; then
    PROFILE_DIR="$(python3 - <<'PY'
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
profile_dir = str(raw.get("browser", {}).get("user_data_dir", "secrets/tm_profile")).strip()
print(profile_dir or "secrets/tm_profile")
PY
)"
    if [ ! -d "${PROFILE_DIR}" ]; then
        echo ""
        echo ">>> Missing ${PROFILE_DIR}"
        echo ">>> Generate it locally: python monitor.py --bootstrap-session"
        echo ">>> Then copy profile data if this VM is remote, and re-run setup."
        exit 1
    fi
elif [ "${SESSION_MODE}" = "cdp_attach" ]; then
    CHROME_PATH="$(python3 - <<'PY'
import os
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
    HOST_PROFILE_DIR="$(python3 - <<'PY'
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
    HOST_PORT="$(python3 - <<'PY'
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
    if [ ! -x "${CHROME_PATH}" ]; then
        echo ""
        echo ">>> Missing Chrome executable: ${CHROME_PATH}"
        echo ">>> Install Google Chrome (or set browser_host.chrome_executable_path) and rerun setup."
        exit 1
    fi
    mkdir -p "${HOST_PROFILE_DIR}"
else
    if [ ! -f "secrets/tm_storage_state.json" ]; then
        echo ""
        echo ">>> Missing secrets/tm_storage_state.json"
        echo ">>> Generate it locally: python monitor.py --bootstrap-session"
        echo ">>> Then copy it to this VM and re-run setup."
        exit 1
    fi
    chmod 600 secrets/tm_storage_state.json
fi

if [ "${SESSION_MODE}" != "cdp_attach" ]; then
    echo "Running doctor checks..."
    if ! python monitor.py --doctor; then
        echo "Doctor checks failed. Fix issues above and rerun setup."
        exit 1
    fi
fi

# Install systemd service
echo "Installing systemd service..."
REPO_DIR="$(pwd)"
SERVICE_USER="$(whoami)"

sudo tee /etc/systemd/system/ticket-monitor.service > /dev/null <<EOF
[Unit]
Description=Ticket Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/venv/bin/python monitor.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

# Keep logs tidy — stdout/stderr go to journald
StandardOutput=journal
StandardError=journal

# Memory guard for free-tier VM (1 GB RAM)
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF

if [ "${SESSION_MODE}" = "cdp_attach" ]; then
sudo tee /etc/systemd/system/ticket-monitor-browser-host.service > /dev/null <<EOF
[Unit]
Description=Ticket Monitor Chrome Host
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${CHROME_PATH} --remote-debugging-port=${HOST_PORT} --user-data-dir=${HOST_PROFILE_DIR} --no-first-run --no-default-browser-check about:blank
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
fi

if [ "${SESSION_MODE}" = "cdp_attach" ]; then
sudo tee /etc/systemd/system/ticket-monitor.service > /dev/null <<EOF
[Unit]
Description=Ticket Monitor
After=network-online.target ticket-monitor-browser-host.service
Wants=network-online.target ticket-monitor-browser-host.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/venv/bin/python monitor.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

# Keep logs tidy — stdout/stderr go to journald
StandardOutput=journal
StandardError=journal

# Memory guard for free-tier VM (1 GB RAM)
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF
fi

sudo systemctl daemon-reload
if [ "${SESSION_MODE}" = "cdp_attach" ]; then
    sudo systemctl enable ticket-monitor-browser-host
    sudo systemctl start ticket-monitor-browser-host
fi
sudo systemctl enable ticket-monitor
sudo systemctl start ticket-monitor

if [ "${SESSION_MODE}" = "cdp_attach" ]; then
    echo "Running doctor checks..."
    if ! python monitor.py --doctor; then
        echo "Doctor checks failed. Fix issues above and rerun setup."
        exit 1
    fi
fi

echo ""
echo "=== Setup complete ==="
echo "Monitor is running. Useful commands:"
echo "  sudo systemctl status ticket-monitor   # Check status"
echo "  sudo journalctl -u ticket-monitor -f   # Follow logs"
echo "  sudo systemctl restart ticket-monitor   # Restart"
echo "  python monitor.py --doctor             # Health check"
echo "  python monitor.py --once               # One cycle"
