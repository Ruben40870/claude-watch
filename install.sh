#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Claude Watch ==="

echo "[1/3] Installing system dependencies..."
sudo apt install -y \
    python3-gi python3-gi-cairo python3-cairo \
    gir1.2-ayatanaappindicator3-0.1 gir1.2-pango-1.0 \
    python3-cryptography python3-dbus \
    gnome-shell-extension-appindicator

echo "[2/3] Installing Playwright..."
pip3 install --user playwright

echo "[3/3] Setting up..."
chmod +x "$SCRIPT_DIR/claude_watch.py"

echo ""
echo "Done!"
echo ""
echo "Note: If this is your first install, log out and back in once to activate"
echo "the GNOME Shell AppIndicator extension, then run the app again."
echo ""

python3 "$SCRIPT_DIR/claude_watch.py"
