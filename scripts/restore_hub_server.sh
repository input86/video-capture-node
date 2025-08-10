#!/bin/bash
set -e

echo "[RESTORE] Restoring Hub Server from repo..."

# Paths
REPO_DIR="$HOME/video-capture-node"
TARGET_DIR="$HOME/hub_server"
DATA_DIR="$HOME/data"

# Stop services
echo "[RESTORE] Stopping hub services..."
sudo systemctl stop hub-api || true
sudo systemctl stop hub-heartbeat || true
sudo systemctl stop tft-ui || true

# Replace hub_server code
echo "[RESTORE] Syncing hub_server folder..."
rm -rf "$TARGET_DIR"
cp -r "$REPO_DIR/hub_server" "$TARGET_DIR"

# Restore systemd service files
echo "[RESTORE] Restoring service files..."
sudo cp "$REPO_DIR/hub_server/services/"*.service /etc/systemd/system/

# Ensure data directory exists
echo "[RESTORE] Ensuring data directory exists..."
mkdir -p "$DATA_DIR"
chmod 755 "$DATA_DIR"

# Install dependencies
echo "[RESTORE] Installing Python dependencies..."
python3 -m venv "$TARGET_DIR/venv"
"$TARGET_DIR/venv/bin/pip" install --upgrade pip
if [ -f "$TARGET_DIR/requirements.txt" ]; then
    "$TARGET_DIR/venv/bin/pip" install -r "$TARGET_DIR/requirements.txt"
fi

# Reload and enable services
echo "[RESTORE] Reloading systemd and enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable hub-api hub-heartbeat tft-ui

# Start services
echo "[RESTORE] Starting services..."
sudo systemctl start hub-api
sudo systemctl start hub-heartbeat
sudo systemctl start tft-ui

echo "[RESTORE] Hub Server restore completed successfully."

