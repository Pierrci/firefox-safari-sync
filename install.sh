#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_TEMPLATE="$SCRIPT_DIR/com.user.firefox-safari-sync.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.firefox-safari-sync.plist"
SYNC_SCRIPT="$SCRIPT_DIR/sync.py"
LOG_DIR="$HOME/Library/Logs/firefox-safari-sync"
STATE_DIR="$HOME/.config/firefox-safari-sync"

echo "=== Firefox → Safari Sync Daemon Installer ==="
echo

# 1. Check Python 3.10+
echo "Checking Python version..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ via Homebrew: brew install python"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "ERROR: Python 3.10+ required, found $PY_VERSION"
    exit 1
fi
echo "  Found Python $PY_VERSION ✓"

# 2. Create venv and install deps
echo "Setting up virtual environment..."
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    python3 -m venv "$SCRIPT_DIR/venv"
fi
"$SCRIPT_DIR/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
echo "  venv ready, lz4 installed ✓"

# 3. Resolve real Python interpreter path
PYTHON_PATH=$("$SCRIPT_DIR/venv/bin/python3" -c "import os,sys; print(os.path.realpath(sys.executable))")
echo "  Python interpreter: $PYTHON_PATH"

# 4. Create directories
mkdir -p "$LOG_DIR" "$STATE_DIR" "$HOME/Library/LaunchAgents"
echo "  Log dir: $LOG_DIR ✓"
echo "  State dir: $STATE_DIR ✓"

# 5. Generate plist from template
echo "Installing LaunchAgent..."
sed \
    -e "s|__PYTHON_PATH__|$SCRIPT_DIR/venv/bin/python3|g" \
    -e "s|__SYNC_SCRIPT_PATH__|$SYNC_SCRIPT|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"
echo "  Plist written to $PLIST_DEST ✓"

# 6. Bootstrap the agent
# Unload first if already loaded (ignore errors)
launchctl bootout "gui/$(id -u)/com.user.firefox-safari-sync" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
echo "  LaunchAgent bootstrapped ✓"

echo
echo "=== Installation complete ==="
echo
echo "REQUIRED: Grant Full Disk Access to the Python interpreter."
echo "  Path: $PYTHON_PATH"
echo
echo "  Steps:"
echo "    1. Open System Settings → Privacy & Security → Full Disk Access"
echo "    2. Click the + button"
echo "    3. Press Cmd+Shift+G, paste: $PYTHON_PATH"
echo "    4. Click Open and enable the toggle"
echo
echo "  SECURITY NOTE: This grants Full Disk Access to the Python interpreter"
echo "  binary, which means any script run by that same Python binary will also"
echo "  have FDA. For a personal machine with a single-purpose venv, this is"
echo "  an acceptable tradeoff."
echo
echo "Verify the daemon is running:"
echo "  tail -f $LOG_DIR/stdout.log"
