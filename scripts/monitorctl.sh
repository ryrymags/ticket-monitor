#!/bin/bash
# Friendly local control commands for the ticket monitor.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${REPO_DIR}/venv/bin/python"
BROWSER_HOST_CTL="${REPO_DIR}/scripts/browser_host.sh"
MONITOR_LABEL="com.ticketmonitor"
GUARDIAN_LABEL="com.ticketmonitor.guardian"
RELOADER_LABEL="com.ticketmonitor.reloader"
BROWSER_HOST_LABEL="com.ticketmonitor.browser-host"
GUI_DOMAIN="gui/$(id -u)"
MONITOR_TARGET="${GUI_DOMAIN}/${MONITOR_LABEL}"
GUARDIAN_TARGET="${GUI_DOMAIN}/${GUARDIAN_LABEL}"
RELOADER_TARGET="${GUI_DOMAIN}/${RELOADER_LABEL}"
BROWSER_HOST_TARGET="${GUI_DOMAIN}/${BROWSER_HOST_LABEL}"
PLIST_DIR="${HOME}/Library/LaunchAgents"
MONITOR_PLIST="${PLIST_DIR}/${MONITOR_LABEL}.plist"
GUARDIAN_PLIST="${PLIST_DIR}/${GUARDIAN_LABEL}.plist"
RELOADER_PLIST="${PLIST_DIR}/${RELOADER_LABEL}.plist"
BROWSER_HOST_PLIST="${PLIST_DIR}/${BROWSER_HOST_LABEL}.plist"
CONFIG_PATH="${REPO_DIR}/config.yaml"

cd "${REPO_DIR}"

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Missing virtualenv python: ${PYTHON_BIN}"
  echo "Run: bash ${REPO_DIR}/deploy/setup_macos.sh"
  exit 1
fi

cmd="${1:-help}"

is_cdp_attach_mode() {
  "${PYTHON_BIN}" - <<'PY' "${CONFIG_PATH}"
import sys, yaml
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
mode = str((raw.get("browser", {}) or {}).get("session_mode", "storage_state")).strip().lower()
print("1" if mode == "cdp_attach" else "0")
PY
}

browser_host_enabled() {
  "${PYTHON_BIN}" - <<'PY' "${CONFIG_PATH}"
import sys, yaml
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
browser = raw.get("browser", {}) or {}
mode = str(browser.get("session_mode", "storage_state")).strip().lower()
host = raw.get("browser_host", {}) or {}
enabled = host.get("enabled", mode == "cdp_attach")
print("1" if bool(enabled) else "0")
PY
}

auth_auto_login_enabled() {
  "${PYTHON_BIN}" - <<'PY' "${CONFIG_PATH}"
import sys, yaml
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
auth = raw.get("auth", {}) or {}
print("1" if bool(auth.get("auto_login_enabled", False)) else "0")
PY
}

ensure_service_loaded() {
  local target="$1"
  local plist="$2"
  if launchctl print "${target}" >/dev/null 2>&1; then
    return 0
  fi
  if [ ! -f "${plist}" ]; then
    return 1
  fi
  launchctl bootstrap "${GUI_DOMAIN}" "${plist}" >/dev/null 2>&1 \
    || launchctl load "${plist}" >/dev/null 2>&1 \
    || true
  launchctl print "${target}" >/dev/null 2>&1
}

is_service_running() {
  local target="$1"
  launchctl print "${target}" 2>/dev/null | grep -q "state = running"
}

wait_for_service_running() {
  local target="$1"
  local timeout_seconds="${2:-20}"
  local interval_seconds=1
  local attempts=$((timeout_seconds / interval_seconds))
  if [ "${attempts}" -lt 1 ]; then
    attempts=1
  fi
  local i
  for ((i=0; i<attempts; i++)); do
    if is_service_running "${target}"; then
      return 0
    fi
    sleep "${interval_seconds}"
  done
  return 1
}

kickstart_monitor_services() {
  launchctl kickstart -k "${MONITOR_TARGET}" >/dev/null 2>&1 || true
  launchctl kickstart -k "${GUARDIAN_TARGET}" >/dev/null 2>&1 || true
  launchctl kickstart -k "${RELOADER_TARGET}" >/dev/null 2>&1 || true
}

start_monitor_services() {
  ensure_service_loaded "${MONITOR_TARGET}" "${MONITOR_PLIST}" || return 1
  ensure_service_loaded "${GUARDIAN_TARGET}" "${GUARDIAN_PLIST}" || true
  ensure_service_loaded "${RELOADER_TARGET}" "${RELOADER_PLIST}" || true
  kickstart_monitor_services
  if wait_for_service_running "${MONITOR_TARGET}" 20; then
    return 0
  fi
  echo "Monitor launchd service did not reach running state in 20s."
  launchctl print "${MONITOR_TARGET}" 2>/dev/null | sed -n '1,40p' || true
  return 1
}

stop_monitor_services() {
  launchctl bootout "${MONITOR_TARGET}" >/dev/null 2>&1 || launchctl unload "${MONITOR_PLIST}" >/dev/null 2>&1 || true
  launchctl bootout "${GUARDIAN_TARGET}" >/dev/null 2>&1 || launchctl unload "${GUARDIAN_PLIST}" >/dev/null 2>&1 || true
  launchctl bootout "${RELOADER_TARGET}" >/dev/null 2>&1 || launchctl unload "${RELOADER_PLIST}" >/dev/null 2>&1 || true
}

ensure_browser_host() {
  if [ "$(is_cdp_attach_mode)" != "1" ]; then
    return 0
  fi
  if [ "$(browser_host_enabled)" != "1" ]; then
    return 0
  fi
  if [ ! -x "${BROWSER_HOST_CTL}" ]; then
    echo "Missing browser host controller: ${BROWSER_HOST_CTL}"
    return 1
  fi
  "${BROWSER_HOST_CTL}" ensure --config "${CONFIG_PATH}"
}

start_all_services() {
  ensure_browser_host || return 1
  start_monitor_services
}

stop_all_services() {
  stop_monitor_services
  if [ "$(is_cdp_attach_mode)" = "1" ] && [ "$(browser_host_enabled)" = "1" ] && [ -x "${BROWSER_HOST_CTL}" ]; then
    "${BROWSER_HOST_CTL}" stop --config "${CONFIG_PATH}" >/dev/null 2>&1 || true
  else
    launchctl bootout "${BROWSER_HOST_TARGET}" >/dev/null 2>&1 || launchctl unload "${BROWSER_HOST_PLIST}" >/dev/null 2>&1 || true
  fi
}

verify_health_json() {
  if ! is_service_running "${MONITOR_TARGET}"; then
    echo "UNHEALTHY"
    echo "Monitor launchd service is not running"
    return 1
  fi

  "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --health-json --config "${CONFIG_PATH}" \
    | "${PYTHON_BIN}" -c '
import json, sys

data = json.load(sys.stdin)
issues = []
mode = str(data.get("browser_session_mode", "")).strip().lower()
if mode == "cdp_attach":
    if not bool(data.get("browser_host_running", False)):
        issues.append("browser_host_running=false")
    if not bool(data.get("cdp_connected", False)):
        issues.append("cdp_connected=false")
events = data.get("events", [])
if not events:
    issues.append("No events configured")
for event in events:
    name = event.get("name", event.get("event_id", "unknown"))
    if event.get("in_outage_state", False):
        issues.append(f"{name}: outage state true")
    blocked = int(event.get("consecutive_blocked", 0))
    if blocked >= 5:
        issues.append(f"{name}: consecutive_blocked={blocked}")
    if bool(event.get("event_check_stale", False)):
        age = event.get("last_check_age_seconds")
        if age is None:
            issues.append(f"{name}: event_check_stale=true")
        else:
            issues.append(f"{name}: event_check_stale=true (age={age}s)")
if issues:
    print("UNHEALTHY")
    for item in issues:
        print(item)
    raise SystemExit(1)
print("HEALTHY")
	'
}

is_cdp_health_failure_output() {
  local output="${1:-}"
  if [ -z "${output}" ]; then
    return 1
  fi
  echo "${output}" | grep -q "^browser_host_running=false$" && return 0
  echo "${output}" | grep -q "^cdp_connected=false$" && return 0
  return 1
}

get_active_auth_pause_until() {
  "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --health-json --config "${CONFIG_PATH}" \
    | "${PYTHON_BIN}" -c '
import json, sys
from datetime import datetime, timezone

data = json.load(sys.stdin)
pause = (data.get("health") or {}).get("auth_pause_until")
if not pause:
    raise SystemExit(1)

try:
    pause_dt = datetime.fromisoformat(pause)
except ValueError:
    print(pause)
    raise SystemExit(0)

if pause_dt.tzinfo is None:
    pause_dt = pause_dt.replace(tzinfo=timezone.utc)

if pause_dt > datetime.now(timezone.utc):
    print(pause_dt.isoformat())
    raise SystemExit(0)
raise SystemExit(1)
'
}

wait_for_healthy() {
  local timeout_seconds="${1:-60}"
  local interval_seconds=5
  local attempts=$((timeout_seconds / interval_seconds))
  if [ "${attempts}" -lt 1 ]; then
    attempts=1
  fi
  local i
  for ((i=0; i<attempts; i++)); do
    if verify_health_json >/dev/null; then
      return 0
    fi
    sleep "${interval_seconds}"
  done
  return 1
}

run_verify_health() {
  echo "=== Verify: services + health + auto-heal ==="
  local start_output=""
  if ! start_output="$(start_all_services 2>&1)"; then
    sleep 2
    if ! start_output="$(start_all_services 2>&1)"; then
      echo "FAIL: Could not start monitor services."
      if [ -n "${start_output}" ]; then
        echo "${start_output}"
      fi
      if [ "$(is_cdp_attach_mode)" = "1" ]; then
        echo "Check Google Chrome install/path and browser host config."
        echo "Run: ${BROWSER_HOST_CTL} status --config ${CONFIG_PATH}"
      fi
      return 1
    fi
  fi

  local health_output=""
  if health_output="$(verify_health_json 2>&1)"; then
    echo "PASS: Monitor is running and healthy."
    return 0
  fi

  if wait_for_healthy 20; then
    echo "PASS: Monitor is running and healthy."
    return 0
  fi

  health_output="$(verify_health_json 2>&1 || true)"
  if [ "$(is_cdp_attach_mode)" = "1" ] && is_cdp_health_failure_output "${health_output}"; then
    echo "FAIL: CDP browser host is not healthy."
    echo "${health_output}" | sed '/^UNHEALTHY$/d'
    echo "Run:"
    echo "  ${BROWSER_HOST_CTL} status --config ${CONFIG_PATH}"
    echo "  ${REPO_DIR}/scripts/monitorctl.sh reauth"
    return 1
  fi

  if [ "$(auth_auto_login_enabled)" = "1" ]; then
    local pause_until=""
    pause_until="$(get_active_auth_pause_until || true)"
    if [ -n "${pause_until}" ]; then
      echo "FAIL: Auto re-auth is paused until ${pause_until}."
      echo "Manual login is required now."
      echo "Run:"
      echo "  ${REPO_DIR}/scripts/monitorctl.sh reauth"
      return 1
    fi
  fi

  echo "Health not clean yet. Running auto-fix..."
  start_all_services
  "${PYTHON_BIN}" "${REPO_DIR}/scripts/guardian.py" --config "${CONFIG_PATH}" --force-fix >/dev/null || true
  "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --restart-browser --config "${CONFIG_PATH}" >/dev/null || true
  start_all_services

  if wait_for_healthy 90; then
    echo "PASS: Auto-fix recovered monitor health."
    return 0
  fi

  echo "FAIL: Monitor still unhealthy after auto-fix."
  echo "Check:"
  echo "  ${REPO_DIR}/scripts/monitorctl.sh status"
  echo "  ${REPO_DIR}/scripts/monitorctl.sh logs"
  echo "Next step:"
  echo "  ${PYTHON_BIN} ${REPO_DIR}/monitor.py --bootstrap-session --config ${CONFIG_PATH}"
  return 1
}

case "${cmd}" in
  status)
    if [ -x "${BROWSER_HOST_CTL}" ]; then
      echo "=== Browser host ==="
      "${BROWSER_HOST_CTL}" status --config "${CONFIG_PATH}" || true
      echo
    fi
    echo "=== Service status ==="
    launchctl print "${MONITOR_TARGET}" | sed -n '1,80p' || true
    echo
    echo "=== Monitor health JSON ==="
    "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --health-json --config "${CONFIG_PATH}"
    echo
    echo "=== Event check freshness ==="
    "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --health-json --config "${CONFIG_PATH}" \
      | "${PYTHON_BIN}" -c '
import json, sys

data = json.load(sys.stdin)
threshold = data.get("event_check_stale_seconds")
for event in data.get("events", []):
    name = event.get("name", event.get("event_id", "unknown"))
    stale = bool(event.get("event_check_stale", False))
    age = event.get("last_check_age_seconds")
    print(f"{name}: stale={stale} age_seconds={age} threshold={threshold}")
'
    ;;

  fix)
    echo "Running guardian remediation..."
    "${PYTHON_BIN}" "${REPO_DIR}/scripts/guardian.py" --config "${CONFIG_PATH}" --force-fix
    ;;

  restart)
    echo "Restarting monitor service..."
    ensure_browser_host || true
    ensure_service_loaded "${MONITOR_TARGET}" "${MONITOR_PLIST}" || true
    launchctl kickstart -k "${MONITOR_TARGET}" >/dev/null 2>&1 || true
    ;;

  start)
    echo "Starting monitor services..."
    start_output=""
    if ! start_output="$(start_all_services 2>&1)"; then
      echo "FAIL: Unable to load or start monitor service."
      if [ -n "${start_output}" ]; then
        echo "${start_output}"
      fi
      exit 1
    fi
    if wait_for_healthy 45; then
      echo "PASS: Services are running and healthy."
      exit 0
    fi
    echo "WARN: Services started but health is not clean yet."
    exit 1
    ;;

  stop)
    echo "Stopping monitor services..."
    stop_all_services
    echo "Stopped: monitor stack (and browser host when configured)."
    ;;

  doctor)
    echo "Stopping services for exclusive doctor-lite check..."
    if [ "$(is_cdp_attach_mode)" = "1" ]; then
      stop_monitor_services
      ensure_browser_host || true
    else
      stop_all_services
    fi
    if ! "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --doctor-lite --config "${CONFIG_PATH}"; then
      echo "Restarting services after doctor-lite failure..."
      start_all_services >/dev/null 2>&1 || true
      exit 1
    fi
    echo "Restarting services..."
    start_all_services >/dev/null 2>&1 || true
    ;;

  reauth)
    echo "=== Re-auth flow (interactive) ==="
    if [ "$(is_cdp_attach_mode)" = "1" ]; then
      echo "Step 0/4: Stopping monitor stack while keeping Chrome host profile active..."
      stop_monitor_services
      ensure_browser_host
    else
      echo "Step 0/4: Stopping services to release profile lock..."
      stop_all_services
    fi

    echo "Step 1/4: Launching Ticketmaster bootstrap session..."
    "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --bootstrap-session --config "${CONFIG_PATH}"

    echo "Step 2/4: Running doctor-lite..."
    "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --doctor-lite --config "${CONFIG_PATH}"

    echo "Step 3/4: Starting services..."
    start_all_services

    echo "Step 4/4: Waiting for healthy state..."
    if wait_for_healthy 90; then
      echo "PASS: Re-auth completed and monitor is healthy."
      exit 0
    fi

    echo "WARN: Re-auth completed but monitor is not yet healthy."
    echo "Check logs with: ${REPO_DIR}/scripts/monitorctl.sh logs"
    exit 1
    ;;

  verify)
    if run_verify_health; then
      exit 0
    fi
    exit 1
    ;;

  verify-webhook)
    if ! run_verify_health; then
      exit 1
    fi

    echo "Stopping services for exclusive browser doctor check..."
    if [ "$(is_cdp_attach_mode)" = "1" ]; then
      stop_monitor_services
      ensure_browser_host || true
    else
      stop_all_services
    fi

    echo "Running end-to-end doctor check (includes Discord test webhook)..."
    if ! "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --doctor --config "${CONFIG_PATH}"; then
      echo "Restarting services after doctor failure..."
      start_all_services >/dev/null 2>&1 || true
      echo "FAIL: Doctor check failed."
      exit 1
    fi

    matrix_failed=0
    echo "Sending webhook sample matrix (Type 1 bingo, Type 2 bingo, non-bingo)..."
    if ! "${PYTHON_BIN}" "${REPO_DIR}/monitor.py" --test-ticket-alert-matrix --config "${CONFIG_PATH}"; then
      matrix_failed=1
    fi

    echo "Restarting services..."
    start_all_services >/dev/null 2>&1 || true
    wait_for_healthy 45 >/dev/null 2>&1 || true
    if [ "${matrix_failed}" -ne 0 ]; then
      echo "FAIL: Webhook sample matrix failed."
      exit 1
    fi
    echo "PASS: End-to-end verify complete (health + doctor + 3 webhook ticket examples)."
    exit 0
    ;;

  logs)
    if [ -f "${REPO_DIR}/logs/browser-host.log" ]; then
      echo "=== browser-host.log ==="
      tail -n 120 "${REPO_DIR}/logs/browser-host.log" || true
      echo
    fi
    if [ -f "${REPO_DIR}/logs/browser-host.launchd.err.log" ]; then
      echo "=== browser-host.launchd.err.log ==="
      tail -n 120 "${REPO_DIR}/logs/browser-host.launchd.err.log" || true
      echo
    fi
    echo "=== monitor.log ==="
    tail -n 120 "${REPO_DIR}/logs/monitor.log" || true
    echo
    echo "=== guardian.log ==="
    tail -n 120 "${REPO_DIR}/logs/guardian.log" || true
    echo
    echo "=== reloader.log ==="
    tail -n 120 "${REPO_DIR}/logs/reloader.log" || true
    echo
    echo "=== guardian.launchd.err.log ==="
    tail -n 120 "${REPO_DIR}/logs/guardian.launchd.err.log" || true
    echo
    echo "=== reloader.launchd.err.log ==="
    tail -n 120 "${REPO_DIR}/logs/reloader.launchd.err.log" || true
    echo
    echo "=== launchd.err.log ==="
    tail -n 120 "${REPO_DIR}/logs/launchd.err.log" || true
    ;;

  help|*)
    echo "Usage: $(basename "$0") {status|start|stop|verify|verify-webhook|fix|restart|doctor|reauth|logs}"
    ;;
esac
