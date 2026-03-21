#!/bin/bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.user.firefox-safari-sync.plist"

echo "=== Firefox → Safari Sync Daemon Uninstaller ==="
echo

# 1. Unload the agent
echo "Stopping LaunchAgent..."
launchctl bootout "gui/$(id -u)/com.user.firefox-safari-sync" 2>/dev/null && \
    echo "  LaunchAgent stopped ✓" || \
    echo "  LaunchAgent was not loaded (already stopped)."

# 2. Remove plist
if [ -f "$PLIST_DEST" ]; then
    rm "$PLIST_DEST"
    echo "  Removed $PLIST_DEST ✓"
else
    echo "  Plist not found at $PLIST_DEST (already removed)."
fi

echo
echo "=== Uninstall complete ==="
echo
echo "Note: The following were NOT removed (delete manually if desired):"
echo "  State:  ~/.config/firefox-safari-sync/state.json"
echo "  Logs:   ~/Library/Logs/firefox-safari-sync/"
echo "  Venv:   ./venv/"
