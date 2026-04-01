#!/bin/bash
# Manage dedicated Chrome browser-host process for CDP attach mode.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${REPO_DIR}/venv/bin/python"
CONFIG_PATH="${REPO_DIR}/config.yaml"
HOST_LABEL="com.ticketmonitor.browser-host"
HOST_TARGET="gui/$(id -u)/${HOST_LABEL}"
HOST_PLIST="${HOME}/Library/LaunchAgents/${HOST_LABEL}.plist"
JSON_OUTPUT=0

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Missing virtualenv python: ${PYTHON_BIN}"
  exit 1
fi

cmd="${1:-status}"
if [ $# -gt 0 ]; then
  shift
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --config)
      CONFIG_PATH="${2:-}"
      shift 2
      ;;
    --json)
      JSON_OUTPUT=1
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

read_host_config() {
  "${PYTHON_BIN}" - <<'PY' "${CONFIG_PATH}" "${REPO_DIR}"
import json
import os
import sys
import yaml

config_path, repo_dir = sys.argv[1], sys.argv[2]
with open(config_path, "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}

browser = raw.get("browser", {}) or {}
mode = str(browser.get("session_mode", "storage_state")).strip().lower()
cdp_endpoint_url = str(browser.get("cdp_endpoint_url", "http://127.0.0.1:9222")).strip()

host = raw.get("browser_host", {}) or {}
enabled = bool(host.get("enabled", mode == "cdp_attach"))
chrome_path = str(
    host.get(
        "chrome_executable_path",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
).strip()
user_data_dir = str(host.get("user_data_dir", "secrets/tm_chrome_profile")).strip() or "secrets/tm_chrome_profile"
port = host.get("remote_debugging_port", 9222)
try:
    port = int(port)
except (TypeError, ValueError):
    port = 9222
if port < 1:
    port = 9222

if not os.path.isabs(user_data_dir):
    user_data_dir = os.path.normpath(os.path.join(repo_dir, user_data_dir))

if not cdp_endpoint_url:
    cdp_endpoint_url = f"http://127.0.0.1:{port}"

print(json.dumps({
    "mode": mode,
    "required": mode == "cdp_attach" and enabled,
    "enabled": enabled,
    "endpoint_url": cdp_endpoint_url,
    "chrome_path": chrome_path,
    "user_data_dir": user_data_dir,
    "port": port,
}))
PY
}

host_cfg_json="$(read_host_config)"

host_cfg_get() {
  local key="$1"
  "${PYTHON_BIN}" - <<'PY' "${host_cfg_json}" "${key}"
import json
import sys
payload = json.loads(sys.argv[1])
key = sys.argv[2]
value = payload.get(key)
if isinstance(value, bool):
    print("1" if value else "0")
elif value is None:
    print("")
else:
    print(str(value))
PY
}

MODE="$(host_cfg_get mode)"
REQUIRED="$(host_cfg_get required)"
ENABLED="$(host_cfg_get enabled)"
ENDPOINT_URL="$(host_cfg_get endpoint_url)"
CHROME_PATH="$(host_cfg_get chrome_path)"
USER_DATA_DIR="$(host_cfg_get user_data_dir)"
REMOTE_PORT="$(host_cfg_get port)"

service_running() {
  launchctl print "${HOST_TARGET}" 2>/dev/null | grep -q "state = running"
}

endpoint_ready() {
  "${PYTHON_BIN}" - <<'PY' "${ENDPOINT_URL}"
import sys
import urllib.error
import urllib.request

endpoint = (sys.argv[1] or "").rstrip("/")
if not endpoint:
    raise SystemExit(1)
url = endpoint + "/json/version"
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        raise SystemExit(0 if int(getattr(response, "status", 0)) == 200 else 1)
except (urllib.error.URLError, ValueError, OSError):
    raise SystemExit(1)
PY
}

start_service() {
  if [ "${REQUIRED}" != "1" ]; then
    return 0
  fi

  if endpoint_ready; then
    return 0
  fi

  if [ ! -f "${HOST_PLIST}" ]; then
    echo "Missing browser-host LaunchAgent: ${HOST_PLIST}"
    return 1
  fi

  launchctl print "${HOST_TARGET}" >/dev/null 2>&1 \
    || launchctl bootstrap "gui/$(id -u)" "${HOST_PLIST}" >/dev/null 2>&1 \
    || launchctl load "${HOST_PLIST}" >/dev/null 2>&1 \
    || true

  if ! service_running; then
    launchctl kickstart -k "${HOST_TARGET}" >/dev/null 2>&1 || true
  fi

  for _ in $(seq 1 40); do
    if endpoint_ready; then
      return 0
    fi
    sleep 0.25
  done

  launchctl kickstart -k "${HOST_TARGET}" >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do
    if endpoint_ready; then
      return 0
    fi
    sleep 0.25
  done

  return 1
}

stop_service() {
  if [ -f "${HOST_PLIST}" ]; then
    launchctl bootout "${HOST_TARGET}" >/dev/null 2>&1 || launchctl unload "${HOST_PLIST}" >/dev/null 2>&1 || true
  fi
}

print_status() {
  local running="0"
  local endpoint="0"
  if service_running; then
    running="1"
  fi
  if endpoint_ready; then
    endpoint="1"
  fi

  if [ "${JSON_OUTPUT}" = "1" ]; then
    "${PYTHON_BIN}" - <<'PY' "${MODE}" "${REQUIRED}" "${ENABLED}" "${running}" "${endpoint}" "${ENDPOINT_URL}" "${CHROME_PATH}" "${USER_DATA_DIR}" "${REMOTE_PORT}"
import json
import sys
print(json.dumps({
    "mode": sys.argv[1],
    "required": sys.argv[2] == "1",
    "enabled": sys.argv[3] == "1",
    "service_running": sys.argv[4] == "1",
    "endpoint_ready": sys.argv[5] == "1",
    "endpoint_url": sys.argv[6],
    "chrome_executable_path": sys.argv[7],
    "user_data_dir": sys.argv[8],
    "remote_debugging_port": int(sys.argv[9]),
}, sort_keys=True))
PY
    return
  fi

  echo "mode: ${MODE}"
  echo "required: $([ "${REQUIRED}" = "1" ] && echo "yes" || echo "no")"
  echo "enabled: $([ "${ENABLED}" = "1" ] && echo "yes" || echo "no")"
  echo "service_running: $([ "${running}" = "1" ] && echo "yes" || echo "no")"
  echo "endpoint_ready: $([ "${endpoint}" = "1" ] && echo "yes" || echo "no")"
  echo "endpoint_url: ${ENDPOINT_URL}"
  echo "chrome_executable_path: ${CHROME_PATH}"
  echo "user_data_dir: ${USER_DATA_DIR}"
  echo "remote_debugging_port: ${REMOTE_PORT}"
}

case "${cmd}" in
  start|ensure)
    if ! start_service; then
      echo "Browser host failed to start."
      exit 1
    fi
    ;;
  stop)
    stop_service
    ;;
  status)
    print_status
    ;;
  *)
    echo "Usage: $(basename "$0") {start|stop|status|ensure} [--config PATH] [--json]"
    exit 1
    ;;
esac
