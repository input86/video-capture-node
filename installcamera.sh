#!/usr/bin/env bash

set -euo pipefail

# installcamera.sh — Camera Node (Pi Zero 2 W) Setup Script
# Usage:
#   ./installcamera.sh [install_directory]
# Example:
#   ./installcamera.sh /home/pi/camera_node

INSTALL_DIR="${1:-/home/pi/camera_node}"

echo "==> Updating system and installing OS-level dependencies..."
sudo apt update
sudo apt install -y   git python3 python3-venv python3-pip   libcamera-dev python3-libcamera   ffmpeg i2c-tools   libjpeg-dev libtiff-dev libavcodec-dev libavformat-dev libswscale-dev libv4l-dev   libzstd-dev libwebp-dev liblzma-dev libjbig-dev libdeflate-dev   libcap-dev

echo "==> Ensuring I2C is enabled..."
sudo raspi-config nonint do_i2c 0

echo "==> Creating install directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "==> Cloning project repo (if not already present)..."
if [ ! -d ".git" ]; then
  git clone git@github.com:input86/video-capture-node.git "$INSTALL_DIR"
fi

echo "==> Removing any existing virtual environment (clean slate)..."
rm -rf "$INSTALL_DIR/venv"

echo "==> Creating Python virtual environment (with system packages)..."
python3 -m venv --system-site-packages venv
source venv/bin/activate

echo "==> Installing Python dependencies into venv..."
pip install --upgrade pip
pip install adafruit-blinka adafruit-circuitpython-vl53l0x gpiozero requests pyyaml

echo "==> Writing default config.yaml (if missing)..."
if [ ! -f config.yaml ]; then
cat > config.yaml <<EOF
hub_url: "http://192.168.0.150:5000"
node_id: "hub-cam01"
auth_token: "YOUR_SHARED_SECRET"
sensor:
  threshold_mm: 1000
  debounce_ms: 200
recording:
  resolution: "1280x720"
  framerate: 30
  duration_s: 5
storage:
  max_clips: 100
  min_free_percent: 10
EOF
fi

echo "==> Creating systemd service..."
SERVICE_FILE=/etc/systemd/system/camera-node.service
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Camera Node Service
After=network.target

[Service]
User=pi
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/src/camera_node.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting camera-node.service..."
sudo systemctl daemon-reload
sudo systemctl enable camera-node
sudo systemctl restart camera-node

echo "✅ Installation complete. Check logs with: sudo journalctl -u camera-node -f"
echo "   Edit config.yaml to customize settings and restart the service after changes."
