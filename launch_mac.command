#!/bin/bash
# =============================================================================
# Ticket Monitor — macOS Launcher
# Double-click this file to open the Ticket Monitor app.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Check setup has been run
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "Setup hasn't been run yet."
    echo "Please double-click  setup_mac.command  first."
    read -p "Press Enter to close..."
    exit 1
fi

# Check tkinter is available in the venv Python (not system Python)
"$SCRIPT_DIR/venv/bin/python3" -c "import tkinter" 2>/dev/null
if [ $? -ne 0 ]; then
    PY_MINOR=$("$SCRIPT_DIR/venv/bin/python3" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
    echo "❌  tkinter is not installed."
    echo ""
    echo "Fix it by running this in Terminal:"
    echo "  brew install python-tk@3.${PY_MINOR}"
    echo ""
    echo "Then double-click this launcher again."
    read -p "Press Enter to close..."
    exit 1
fi

# Activate the venv from THIS project (not any other)
source "$SCRIPT_DIR/venv/bin/activate"
python3 "$SCRIPT_DIR/app.py"

EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌  The app exited with an error (code $EXIT_CODE). See above for details."
    echo "    If packages are missing, try running setup_mac.command again."
    read -p "Press Enter to close..."
fi
