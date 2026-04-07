#!/bin/bash
# =============================================================================
# Ticket Monitor — macOS Setup Script
# Double-click this file to set up the monitor for the first time.
# =============================================================================

# Change to the folder this script lives in (so relative paths work)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         Ticket Monitor — Setup           ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Setting up in: $SCRIPT_DIR"
echo ""

# ── Find the best Python 3 (prefer Homebrew 3.12+ which bundles Tk 8.6+) ─────
PYTHON3=""
# Prefer Homebrew/explicit versioned binaries first (they bundle Tk 8.6+)
for candidate in \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3.10 \
    /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 \
    /usr/local/bin/python3.10 \
    python3.13 \
    python3.12 \
    python3.11 \
    python3.10 \
    python3; do
    if command -v "$candidate" &>/dev/null 2>&1; then
        # Must be at least Python 3.10
        PY_MINOR_CHECK=$("$candidate" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        PY_MAJOR_CHECK=$("$candidate" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        if [ "$PY_MAJOR_CHECK" = "3" ] && [ "${PY_MINOR_CHECK:-0}" -ge "10" ] 2>/dev/null; then
            PYTHON3="$candidate"
            break
        fi
    fi
done

# Fall back to any python3
if [ -z "$PYTHON3" ]; then
    if command -v python3 &>/dev/null; then
        PYTHON3="python3"
    else
        echo "❌  Python 3 is not installed."
        echo ""
        echo "Please install Python 3.12 (recommended) from Homebrew:"
        echo "  brew install python@3.12 python-tk@3.12"
        echo ""
        echo "Or download from:  https://www.python.org/downloads/"
        echo ""
        read -p "Press Enter to close..."
        exit 1
    fi
fi

PY_VERSION=$("$PYTHON3" --version 2>&1)
PY_MINOR=$("$PYTHON3" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
echo "✅  Found $PY_VERSION  ($PYTHON3)"
echo ""

# ── Check for tkinter (common issue with Homebrew Python) ────────────────────
echo "⏳  Checking tkinter (required for GUI)..."
"$PYTHON3" -c "import tkinter" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "⚠️   tkinter not found — this is common with Homebrew Python."
    echo ""
    if command -v brew &>/dev/null; then
        echo "⏳  Installing python-tk via Homebrew..."
        brew install "python-tk@3.${PY_MINOR}" 2>/dev/null
        if [ $? -ne 0 ]; then
            brew install python-tk 2>/dev/null
        fi
        # Re-check
        "$PYTHON3" -c "import tkinter" 2>/dev/null
        if [ $? -ne 0 ]; then
            echo ""
            echo "❌  Could not install tkinter automatically."
            echo ""
            echo "Please run this command in Terminal, then re-run setup:"
            echo "  brew install python-tk@3.${PY_MINOR}"
            echo ""
            echo "Or download Python from python.org (includes tkinter by default):"
            echo "  https://www.python.org/downloads/"
            read -p "Press Enter to close..."
            exit 1
        fi
        echo "✅  tkinter installed."
    else
        echo "❌  Homebrew not found. Please either:"
        echo "    1. Install Homebrew (https://brew.sh) then run:  brew install python-tk@3.${PY_MINOR}"
        echo "    2. OR download Python from python.org (includes tkinter):  https://www.python.org/downloads/"
        echo ""
        read -p "Press Enter to close..."
        exit 1
    fi
else
    echo "✅  tkinter is available."
fi
echo ""

# ── Delete old venv if it was built with the wrong Python ────────────────────
if [ -d "venv" ]; then
    VENV_PY=$(venv/bin/python3 --version 2>&1)
    if [ "$VENV_PY" != "$PY_VERSION" ]; then
        echo "⏳  Existing venv uses a different Python ($VENV_PY vs $PY_VERSION). Rebuilding..."
        rm -rf venv
    fi
fi

# ── Create virtual environment ───────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "⏳  Creating virtual environment..."
    "$PYTHON3" -m venv venv
    if [ $? -ne 0 ]; then
        echo "❌  Failed to create virtual environment."
        read -p "Press Enter to close..."
        exit 1
    fi
    echo "✅  Virtual environment created."
else
    echo "✅  Virtual environment already exists."
fi
echo ""

# ── Activate and install packages ────────────────────────────────────────────
echo "⏳  Installing Python packages (this may take a minute)..."
source "$SCRIPT_DIR/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌  Package installation failed. Check your internet connection and try again."
    read -p "Press Enter to close..."
    exit 1
fi
echo "✅  Python packages installed."
echo ""

# ── Install Playwright browser ────────────────────────────────────────────────
echo "⏳  Installing browser engine (Chromium)..."
python3 -m playwright install chromium 2>/dev/null
if [ $? -ne 0 ]; then
    echo "⚠️   Playwright browser install had issues — the browser may not work."
    echo "     Check your internet connection and re-run setup to try again."
fi
echo "✅  Browser engine ready."
echo ""

# ── Make launch script executable ────────────────────────────────────────────
chmod +x "$SCRIPT_DIR/launch_mac.command" 2>/dev/null

# ── Done ─────────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════╗"
echo "║           Setup Complete! 🎉             ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo "  1. Double-click  launch_mac.command  to open the app"
echo "  2. Add your concert URL in the Events tab"
echo "  3. Set your preferences and Discord webhook"
echo "  4. Hit Start Monitor — and relax!"
echo ""
read -p "Press Enter to close this window..."
