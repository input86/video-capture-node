#!/usr/bin/env bash
set -euo pipefail

# Camera Node installer (mirrors your code; only config & IP are interactive)
# Usage: ./installcamera.sh [install_dir=/home/pi/camera_node] [git_remote]

INSTALL_DIR=${1:-/home/pi/camera_node}
GIT_REMOTE=${2:-}

PI_USER=${SUDO_USER:-pi}
PI_HOME=$(eval echo "~$PI_USER")

bold(){ printf "\e[1m%s\e[0m\n" "$*"; }
ok(){ printf "\e[32m[OK]\e[0m %s\n" "$*"; }
warn(){ printf "\e[33m[WARN]\e[0m %s\n" "$*"; }
note(){ printf "\n\e[1m[NOTE]\e[0m %s\n" "$*"; }
err(){ printf "\e[31m[ERR]\e[0m %s\n" "$*"; }

confirm(){ read -r -p "${1:-Proceed?} [y/N]: " REPLY || true; [[ "$REPLY" =~ ^[Yy](es)?$ ]]; }

bold "== Camera Node Installer =="

# 1) OS deps
bold "1) Installing OS packages…"
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  python3-libcamera python3-picamera2 libcamera-apps \
  ffmpeg i2c-tools python3-rpi.gpio \
  dhcpcd5 git curl
ok "Base packages installed."

# 2) Groups & I2C
bold "2) Adding $PI_USER to groups & enabling I2C…"
sudo usermod -aG video,i2c,gpio "$PI_USER" || true
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_i2c 0 || true
fi
ok "Groups set; I2C enabled (reboot may be needed if just enabled)."

# 3) Prepare project dir
bold "3) Preparing install directory: $INSTALL_DIR"
if [[ -d "$INSTALL_DIR" ]]; then
  warn "$INSTALL_DIR exists — leaving your files intact."
else
  mkdir -p "$INSTALL_DIR/src" "$INSTALL_DIR/queue"
  sudo chown -R "$PI_USER":"$PI_USER" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 4) Optional git sync
if [[ -n "$GIT_REMOTE" ]]; then
  bold "4) Syncing from $GIT_REMOTE…"
  if [[ -d .git ]]; then
    git fetch --all || true
    git reset --hard origin/main || true
  else
    git init
    git remote add origin "$GIT_REMOTE" || true
    git fetch origin || true
    git checkout -b main origin/main || git checkout -b main || true
  fi
  ok "Git sync complete."
else
  note "No git remote provided (skipping)."
fi

# Verify required files are present
missing=0
for f in "src/camera_node.py" "heartbeat_client.py"; do
  if [[ ! -f "$INSTALL_DIR/$f" ]]; then
    err "Missing required file: $f"
    missing=1
  fi
done
[[ $missing -eq 1 ]] && { err "Please place your code files, then re-run."; exit 1; }

# 5) Python venv with system site packages
bold "5) Creating venv (system-site-packages) & installing Python deps…"
if [[ ! -d venv ]]; then
  python3 -m venv --system-site-packages venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip
pip install --no-cache-dir \
  requests pyyaml gpiozero adafruit-circuitpython-vl53l0x adafruit-blinka
ok "Python deps installed."

# 6) Gather config (hub_url, node_id, token) and write config.yaml
bold "6) Writing config.yaml"
read -r -p "Hub URL [http://<hub-ip>:5000]: " HUB_URL || true
HUB_URL=${HUB_URL:-http://<hub-ip>:5000}

read -r -p "Node ID [cam01]: " NODE_ID || true
NODE_ID=${NODE_ID:-cam01}

read -r -p "Auth Token (required): " AUTH_TOKEN || true
[[ -z "${AUTH_TOKEN:-}" ]] && { err "Auth Token required."; exit 1; }

# If config.yaml exists, keep non-core fields; we’ll rewrite core keys
if [[ -f config.yaml ]]; then
  warn "config.yaml exists — updating hub_url/node_id/auth_token, keeping other keys."
  # naive in-place update for the 3 keys; preserves the rest
  tmpcfg=$(mktemp)
  awk -v hub="$HUB_URL" -v nid="$NODE_ID" -v tok="$AUTH_TOKEN" '
    BEGIN{h=0;n=0;a=0}
    /^hub_url:/ {$0="hub_url: \""hub"\"";h=1}
    /^node_id:/ {$0="node_id: \""nid"\"";n=1}
    /^auth_token:/ {$0="auth_token: \""tok"\"";a=1}
    {print}
    END{
      if(!h) print "hub_url: \""hub"\"";
      if(!n) print "node_id: \""nid"\"";
      if(!a) print "auth_token: \""tok"\"";
    }' config.yaml > "$tmpcfg" && mv "$tmpcfg" config.yaml
else
  cat > config.yaml <<EOF
hub_url: "$HUB_URL"
node_id: "$NODE_ID"
auth_token: "$AUTH_TOKEN"

sensor:
  threshold_mm: 1000
  debounce_ms: 200

recording:
  resolution: "1280x720"
  framerate: 30
  duration_s: 5

storage:
  min_free_percent: 10

heartbeat_interval_sec: 10
EOF
fi
ok "config.yaml ready."

# 7) Install systemd units (match your provided content, but dynamic paths)
bold "7) Installing systemd services…"

sudo tee /etc/systemd/system/camera-heartbeat.service >/dev/null <<EOF
[Unit]
Description=Camera Heartbeat Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PI_USER
WorkingDirectory=$INSTALL_DIR
Environment=CN_CONFIG=$INSTALL_DIR/config.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/heartbeat_client.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/camera-node.service >/dev/null <<EOF
[Unit]
Description=Camera Node Service
After=network.target

[Service]
User=$PI_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/src/camera_node.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable camera-node camera-heartbeat
sudo systemctl restart camera-node camera-heartbeat
ok "Services enabled & restarted."

# 8) Optional static IP via dhcpcd (staged; asks before applying)
bold "8) Optional static IP setup (dhcpcd)"
if confirm "Prepare a static IP now (apply only if you confirm a restart)?"; then
  read -r -p "Interface [eth0 or wlan0] (default eth0): " IFACE || true
  IFACE=${IFACE:-eth0}
  read -r -p "Static IP with CIDR (e.g. 192.168.1.50/24): " STATIC_CIDR || true
  read -r -p "Router/Gateway IP (e.g. 192.168.1.1): " ROUTER_IP || true
  read -r -p "DNS (comma-separated, e.g. 1.1.1.1,8.8.8.8) [optional]: " DNS_LIST || true

  sudo install -m 644 -T /etc/dhcpcd.conf "/etc/dhcpcd.conf.backup-$(date +%Y%m%d%H%M%S)" || true
  sudo sed -i '/^# CAMNODE static IP START$/,/# CAMNODE static IP END$/{d}' /etc/dhcpcd.conf

  sudo tee -a /etc/dhcpcd.conf >/dev/null <<EOF

# CAMNODE static IP START
interface $IFACE
static ip_address=$STATIC_CIDR
static routers=$ROUTER_IP
$( [[ -n "$DNS_LIST" ]] && echo "static domain_name_servers=${DNS_LIST// /}" )
# CAMNODE static IP END
EOF

  # Clear old dhcpcd leases
  sudo rm -f /var/lib/dhcpcd5/*.lease || true
  ok "Static IP block written. Old leases cleared."

  if confirm "Apply static IP now by restarting dhcpcd (may drop current SSH)?"; then
    sudo systemctl restart dhcpcd
    ok "dhcpcd restarted — IP may have changed."
  else
    note "Static IP will apply on next reboot or when you run: sudo systemctl restart dhcpcd"
  fi
else
  note "Skipping static IP setup."
fi

bold "Done ✅"
echo "Quick checks:"
echo "  journalctl -u camera-node -n 50 --no-pager"
echo "  journalctl -u camera-heartbeat -n 50 --no-pager"
echo "If you just got added to groups or enabled I2C, consider: sudo reboot"
