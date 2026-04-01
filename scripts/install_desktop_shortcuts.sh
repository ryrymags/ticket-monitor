#!/bin/bash
# Install/update Desktop .command shortcuts for the ticket monitor controls.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MONITORCTL="${REPO_DIR}/scripts/monitorctl.sh"
DESKTOP_DIR="${HOME}/Desktop"

if [ ! -x "${MONITORCTL}" ]; then
  echo "Missing executable monitorctl script: ${MONITORCTL}"
  echo "Run: chmod +x ${MONITORCTL}"
  exit 1
fi

mkdir -p "${DESKTOP_DIR}"

write_shortcut() {
  local file_name="$1"
  local command_name="$2"
  local title="$3"
  local english_help="$4"
  local healthy_hint="$5"
  local fix_hint="$6"
  local target="${DESKTOP_DIR}/${file_name}"

  cat > "${target}" <<EOF
#!/bin/bash
set +e
clear
echo "=== ${title} ==="
echo "${english_help}"
if [ -n "${healthy_hint}" ]; then
  echo "Healthy sign: ${healthy_hint}"
fi
if [ -n "${fix_hint}" ]; then
  echo "If not healthy: ${fix_hint}"
fi
echo
echo "Technical command:"
echo "  ${MONITORCTL} ${command_name}"
echo "------------------------------------------------------------"
echo
"${MONITORCTL}" ${command_name}
cmd_status=\$?
echo
if [ "\${cmd_status}" -eq 0 ]; then
  echo "Result: PASS"
else
  echo "Result: FAIL (exit code \${cmd_status})"
fi
echo
read -r -n 1 -s -p "Press any key to close..."
echo
exit "\${cmd_status}"
EOF
  chmod +x "${target}"
}

write_shortcut \
  "Ticket Monitor Status.command" \
  "status" \
  "Ticket Monitor Status" \
  "This shows what is currently running and current health for both event nights." \
  "You should see browser host healthy, monitor healthy, and both events not stale." \
  "If any section says unhealthy/stale, run the Verify shortcut next."
write_shortcut \
  "Ticket Monitor Start.command" \
  "start" \
  "Ticket Monitor Start" \
  "This starts the monitor services (and Chrome host in CDP mode)." \
  "Result PASS means services started and health is clean." \
  "If FAIL, run Verify for diagnostics."
write_shortcut \
  "Ticket Monitor Stop.command" \
  "stop" \
  "Ticket Monitor Stop" \
  "This stops the monitor stack. In CDP mode it also stops browser host if configured." \
  "Result PASS means stop command completed." \
  "Use Start when you are ready to resume monitoring."
write_shortcut \
  "Ticket Monitor Verify.command" \
  "verify" \
  "Ticket Monitor Verify" \
  "This is your main health check: it validates services, monitor health, and recovery behavior." \
  "Result PASS means monitor is healthy and checks should continue normally." \
  "If FAIL once, run Verify again immediately; then follow any next-step command shown."
write_shortcut \
  "Ticket Monitor Verify Webhook.command" \
  "verify-webhook" \
  "Ticket Monitor Verify + Webhook Test" \
  "This runs full verification and sends 3 Discord ticket examples: LOGE bingo, budget bingo, and non-bingo." \
  "Result PASS means monitor + webhook path are working end-to-end with all sample alert styles." \
  "If FAIL, read the command output and run Reauth or Doctor as suggested."
write_shortcut \
  "Ticket Monitor Fix.command" \
  "fix" \
  "Ticket Monitor Auto-Fix" \
  "This triggers guardian remediation logic to recover unhealthy state." \
  "Result PASS means remediation command completed." \
  "After this, run Verify to confirm final health."
write_shortcut \
  "Ticket Monitor Restart.command" \
  "restart" \
  "Ticket Monitor Restart" \
  "This restarts the main monitor service." \
  "Result PASS means restart command completed." \
  "Run Verify after restart if you want a full health check."
write_shortcut \
  "Ticket Monitor Reauth.command" \
  "reauth" \
  "Ticket Monitor Reauth (Manual Login Flow)" \
  "Use this when Ticketmaster needs you to log in again in the dedicated Chrome profile." \
  "Result PASS means reauth flow completed and monitor returned healthy." \
  "If FAIL, run Status and Logs to see the exact blocker."
write_shortcut \
  "Ticket Monitor Logs.command" \
  "logs" \
  "Ticket Monitor Logs" \
  "This shows recent log output from browser host, monitor, guardian, and reloader." \
  "Healthy behavior usually shows repeating checks for both nights with no crash loops." \
  "Look for recent ERROR lines to identify what to fix."

echo "Desktop shortcuts installed/updated in: ${DESKTOP_DIR}"
