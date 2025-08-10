#!/bin/bash
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "[Auto-update] Fetching latest from GitHub..."
git fetch origin main
git reset --hard origin/main

echo "[Auto-update] Reinstalling dependencies..."
if [ -f installserver.sh ]; then
    chmod +x installserver.sh
    ./installserver.sh "$REPO_DIR"
elif [ -f installcamera.sh ]; then
    chmod +x installcamera.sh
    ./installcamera.sh "$REPO_DIR"
fi

echo "[Auto-update] Restarting services..."
if systemctl list-units --type=service | grep -q hub-api; then
    sudo systemctl restart hub-api hub-heartbeat tft-ui
elif systemctl list-units --type=service | grep -q camera-node; then
    sudo systemctl restart camera-node camera-heartbeat
fi

echo "[Auto-update] Done."
