#!/bin/bash
set -euo pipefail

echo "[1/9] Installing dhcpcd5 and setting static IP..."

# Ensure dhcpcd is installed and configured for static IP
sudo apt update
sudo apt install -y dhcpcd5 git

sudo tee -a /etc/dhcpcd.conf <<'EOF'

interface wlan0
static ip_address=192.168.0.150/24
static routers=192.168.0.1
static domain_name_servers=192.168.0.1 8.8.8.8
EOF

sudo systemctl enable dhcpcd
sudo systemctl restart dhcpcd

echo "[2/9] Installing TFT driver (disables HDMI)..."
cd ~
git clone https://github.com/goodtft/LCD-show.git || true
cd LCD-show
chmod +x LCD35-show
sudo ./LCD35-show

echo "[3/9] Installing Python + dev packages..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip sqlite3 libatlas-base-dev python3-tk xserver-xorg xinit unclutter

echo "[4/9] Setting up venv and installing deps..."
cd ~/video-capture-node/hub_server
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install flask gunicorn pyyaml RPi.GPIO

echo "[5/9] Running migrate to initialize database..."
venv/bin/python migrate.py

echo "[6/9] Installing systemd services..."
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hub-api storage-monitor tft-ui

echo "[7/9] Creating update.sh script..."
cat > update.sh <<'UPD'
#!/bin/bash
cd ~/video-capture-node
git pull
cd hub_server
source venv/bin/activate
pip install -r requirements.txt || true
sudo systemctl restart hub-api storage-monitor tft-ui
UPD
chmod +x update.sh

echo "[8/9] Adding auto-update cron job (2am)..."
( crontab -l 2>/dev/null; echo "0 2 * * * /home/pi/video-capture-node/hub_server/update.sh >> /home/pi/hub_update.log 2>&1" ) | crontab -

echo "[9/9] Done! Reboot to complete setup."
