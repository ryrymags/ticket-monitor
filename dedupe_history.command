#!/bin/bash
# =============================================================================
# Ticket Monitor — History De-duper (macOS)
# Collapses repeat detections of the same seats+price in ticket_history.json
# into one row each. Backs up the original first. Safe to re-run.
#
# Run it either way:
#   • Double-click this file in Finder, OR
#   • In Terminal:  ./dedupe_history.command   (or: bash dedupe_history.command)
#
# TIP: restart the monitor onto the latest code first, then run this — otherwise
# the old build keeps appending duplicates while it runs.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer the project venv; fall back to system python3 (the de-duper only needs
# the standard library, so it works either way).
if [ -x "$SCRIPT_DIR/venv/bin/python3" ]; then
    PY="$SCRIPT_DIR/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "❌  Could not find python3. Run setup_mac.command first."
    read -p "Press Enter to close..."
    exit 1
fi

echo "🧹  De-duping ticket history..."
echo ""
"$PY" "$SCRIPT_DIR/scripts/dedupe_history.py"
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -ne 0 ]; then
    echo "❌  The de-duper exited with an error (code $EXIT_CODE). See above."
else
    echo "✅  Done. Your original was backed up alongside ticket_history.json."
fi
read -p "Press Enter to close..."
