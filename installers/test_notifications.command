#!/bin/bash
# =============================================================================
# Ticket Monitor — Test Notifications (macOS)
# Sends a sample BINGO ticket alert to BOTH Discord and ntfy (your friends'
# phones) so everyone can confirm notifications arrive and work. The ntfy push
# is tappable (opens Ticketmaster) and carries an "Open Ticketmaster" button.
#
# Heads up: this pings EVERYONE subscribed to the ntfy topic. It's a sample
# alert, not a real ticket drop.
#
# Run it either way:
#   • Double-click this file in Finder, OR
#   • In Terminal:  ./test_notifications.command
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Check setup has been run
if [ ! -d "$REPO_ROOT/venv" ]; then
    echo "Setup hasn't been run yet."
    echo "Please double-click  installers/setup_mac.command  first."
    read -p "Press Enter to close..."
    exit 1
fi

# Activate the venv from THIS project (not any other)
source "$REPO_ROOT/venv/bin/activate"

echo "📨  Sending a sample ticket alert to Discord and ntfy..."
echo ""
python3 "$REPO_ROOT/monitor.py" --test-ticket-alert
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -ne 0 ]; then
    echo "❌  The test exited with an error (code $EXIT_CODE). See above for details."
else
    echo "✅  Sent. Check Discord and your friends' phones (ntfy app)."
fi
read -p "Press Enter to close..."
