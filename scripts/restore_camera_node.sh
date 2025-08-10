#!/bin/bash
# restore_camera_node.sh
# Restores a Raspberry Pi Zero 2 W camera node from the latest repo backup

set -e

echo "=== Restoring Camera Node from GitHub Backup ==="

# Stop running services
sudo systemctl stop camera-node.service || true
sudo systemctl stop camera-heartbeat.service || true

# Backup old installation
BACKUP_DIR=~/camera_node_backup_$(date +%Y%m%d_%H%M%S)
if [ -d ~/camera_node ]; then
    echo "Backing up existing ~/camera_node to $BACKUP_DIR"
    mv ~/camera_node "$BACKUP_DIR"
fi

# Ensure scripts/ exists in the repo
mkdir -p ~/video-capture-node/scripts

# Clone or update repo
if [ ! -d ~/video-capture-node/.git ]; then
    echo "Cloning repo fresh..."
    git clone git@github.com:input86/video-capture-node.git ~/video-capture-node
else
    echo "Updating existing repo..."
    cd ~/video-capture-node
    git reset --hard
    git pull
fi

# Deploy from repo to ~/camera_node
cp -r ~/video-capture-node/camera_node ~/camera_node

# Install services
echo "Installing systemd services..."
sudo cp ~/camera_node/services/camera-node.service /etc/systemd/system/
sudo cp ~/camera_node/services/camera-heartbeat.service /etc/systemd/system/

# Reload and enable services
sudo systemctl daemon-reload
sudo systemctl enable camera-node.service
sudo systemctl enable camera-heartbeat.service

# Create and activate venv
echo "Setting up Python venv..."
cd ~/camera_node
python3 -m venv venv
source venv/bin/activate

# Install dependencies from requirements.txt if present
if [ -f requirements.txt ]; then
    pip install --upgrade pip
    pip install -r requirements.txt
fi

# Start services
sudo systemctl start camera-node.service
sudo systemctl start camera-heartbeat.service

echo "=== Camera Node Restore Complete ==="