#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Clone/Reinstall Camera Node from backed-up runtime (camera_runtime/)
# - Works for new devices (cam02, cam03, ...) or reimage of cam01
# - Idempotent; safe to re-run
#
# Optional env overrides (non-interactive):
#   NEW_NODE_ID=cam02 NEW_HOSTNAME=hub-cam02 APPLY_STATIC_IP=1 IFACE=eth0 STATIC_CIDR=192.168.1.52/24 ROUTER_IP=192.168.1.1 DNS_LIST="1.1.1.1,8.8.8.8" \
#   REGEN_SSH_KEYS=1 HUB_URL=http://hub-server:5000 AUTH_TOKEN=MYTOKEN \
#   ~/projects/video-capture-node/scripts/install_or_clone_camera_from_backup.sh
# ============================================================

# --- Repo & paths ---
REPO_SSH_DEFAULT="git@github.com:input86/video-capture-node.git"
REPO_SSH="${REPO_SSH:-$REPO_SSH_DEFAULT}"
REPO_DIR="${REPO_DIR:-$HOME/projects/video-capture-node}"

SRC_RUNTIME_DIR="$REPO_DIR/camera_runtime"   # primary source (your live backup)
SRC_FALLBACK_DIR="$REPO_DIR/camera_node"     # fallback if runtime mirror missing
SERVICES_DIR="$REPO_DIR/services/camera"

INSTALL_DIR="${INSTALL_DIR:-$HOME/camera_node}"  # where runtime will live

# --- Services we manage ---
SERVICES=(camera-node camera-heartbeat)

# --- Optional customizations (can be provided via env) ---
NEW_NODE_ID="${NEW_NODE_ID:-}"           # e.g., cam02
NEW_HOSTNAME="${NEW_HOSTNAME:-}"         # e.g., hub-cam02

APPLY_STATIC_IP="${APPLY_STATIC_IP:-0}"  # 1 to configure dhcpcd static IP
IFACE="${IFACE:-eth0}"
STATIC_CIDR="${STATIC_CIDR:-}"           # e.g., 192.168.1.52/24
ROUTER_IP="${ROUTER_IP:-}"               # e.g., 192.168.1.1
DNS_LIST="${DNS_LIST:-}"                 # e.g., 1.1.1.1,8.8.8.8

REGEN_SSH_KEYS="${REGEN_SSH_KEYS:-0}"    # 1 to generate fresh SSH keys
HUB_URL="${HUB_URL:-}"                   # override hub_url in config.yaml
AUTH_TOKEN="${AUTH_TOKEN:-}"             # override auth_token in config.yaml

# ------------------------------------------------------------
say() { printf "\e[1m%s\e[0m\n" "$*"; }
ok()  { printf "\e[32m[OK]\e[0m %s\n" "$*"; }
warn(){ printf "\e[33m[WARN]\e[0m %s\n" "$*"; }
err() { printf "\e[31m[ERR]\e[0m %s\n" "$*"; }
ask() { read -r -p "$* " REPLY || true; echo "$REPLY"; }

# ------------------------------------------------------------
say "== Camera Node reinstall/clone from backup =="

# 0) Ensure repo present & on main
if [ ! -d "$REPO_DIR/.git" ]; then
  say "Cloning repo to $REPO_DIR"
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --tags --prune --prune-tags
git checkout main || git checkout -b main
git pull --rebase || true
ok "Repo synced to main."

# 1) Stop services if they exist (avoid file locks)
say "Stopping services if running..."
for s in "${SERVICES[@]}"; do
  sudo systemctl stop "$s" 2>/dev/null || true
done
ok "Services stopped (if present)."

# 2) Choose source (prefer camera_runtime/)
SRC_DIR=""
if [ -d "$SRC_RUNTIME_DIR" ]; then
  SRC_DIR="$SRC_RUNTIME_DIR"
  ok "Using source: $SRC_RUNTIME_DIR"
elif [ -d "$SRC_FALLBACK_DIR" ]; then
  SRC_DIR="$SRC_FALLBACK_DIR"
  warn "Using fallback source: $SRC_FALLBACK_DIR (no runtime mirror found)"
else
  err "No source found at $SRC_RUNTIME_DIR or $SRC_FALLBACK_DIR"
  exit 1
fi

# 3) OS packages (idempotent)
say "Installing base OS packages..."
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  python3-libcamera python3-picamera2 libcamera-apps \
  ffmpeg i2c-tools python3-rpi.gpio \
  dhcpcd5 git curl
sudo usermod -aG video,i2c,gpio "$USER" || true
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_i2c 0 || true
fi
ok "Base packages ready."

# 4) Restore files to INSTALL_DIR
say "Syncing files → $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -av --delete \
  --exclude ".git/" \
  --exclude "venv/" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude ".mypy_cache/" \
  --exclude ".pytest_cache/" \
  --exclude "*.pyc" \
  --exclude ".env" --exclude "*.env" \
  "$SRC_DIR/" "$INSTALL_DIR/"
sudo chown -R "$USER:$USER" "$INSTALL_DIR"
ok "Files in place."

# 5) Ensure required files exist
missing=0
for f in "src/camera_node.py" "heartbeat_client.py" "config.yaml"; do
  [ -f "$INSTALL_DIR/$f" ] || { err "Missing $INSTALL_DIR/$f"; missing=1; }
done
[ $missing -eq 0 ] || { err "Required files missing — fix repo and re-run."; exit 1; }

# 6) Update config.yaml (node_id/hub_url/token) if provided
say "Updating config.yaml as requested..."
CFG="$INSTALL_DIR/config.yaml"
TMPCFG="$(mktemp)"
awk -v nid="$NEW_NODE_ID" -v hub="$HUB_URL" -v tok="$AUTH_TOKEN" '
  function set(k,v){
    # print key: "value" if provided
    if (v != "") printf "%s: \"%s\"\n", k, v; else print $0;
  }
  BEGIN{had_nid=0;had_hub=0;had_tok=0}
  /^node_id:/   { if (nid!=""){ set("node_id",nid); had_nid=1 } else print; next }
  /^hub_url:/   { if (hub!=""){ set("hub_url",hub); had_hub=1 } else print; next }
  /^auth_token:/{ if (tok!=""){ set("auth_token",tok); had_tok=1 } else print; next }
  { print }
  END{
    if(nid!="" && !had_nid)   printf "node_id: \"%s\"\n", nid;
    if(hub!="" && !had_hub)   printf "hub_url: \"%s\"\n", hub;
    if(tok!="" && !had_tok)   printf "auth_token: \"%s\"\n", tok;
  }
' "$CFG" > "$TMPCFG" && mv "$TMPCFG" "$CFG"

if [ -z "${NEW_NODE_ID:-}" ] || [ -z "${AUTH_TOKEN:-}" ] || [ -z "${HUB_URL:-}" ]; then
  echo
  echo "Current config essentials:"
  grep -E '^(node_id|hub_url|auth_token):' "$CFG" || true
  echo
  REPLY="$(ask "Edit config.yaml now? [y/N]:")"
  if [[ "$REPLY" =~ ^[Yy] ]]; then
    ${EDITOR:-nano} "$CFG"
  fi
fi
ok "config.yaml ready."

# 7) Python venv & deps
say "Creating venv & installing deps..."
python3 -m venv --system-site-packages "$INSTALL_DIR/venv" || true
# shellcheck disable=SC1091
source "$INSTALL_DIR/venv/bin/activate"
pip install -U pip wheel

# Prefer the latest pip-freeze record from backups for exact versions
LATEST_FREEZE="$(ls -1t "$REPO_DIR"/backups/camera/pip-freeze-*.txt 2>/dev/null | head -n1 || true)"
if [ -n "${LATEST_FREEZE:-}" ] && [ -f "$LATEST_FREEZE" ]; then
  say "Installing from frozen deps: $(basename "$LATEST_FREEZE")"
  pip install -r "$LATEST_FREEZE" || {
    warn "Exact frozen install failed; installing minimal deps."
    pip install requests pyyaml gpiozero adafruit-circuitpython-vl53l0x adafruit-blinka
  }
else
  say "No pip-freeze found; installing minimal deps."
  pip install requests pyyaml gpiozero adafruit-circuitpython-vl53l0x adafruit-blinka
fi
deactivate
ok "Python deps installed."

# 8) Install/refresh systemd units
say "Installing systemd services..."
install_unit_if_present() {
  local src="$1"
  local base; base="$(basename "$src")"
  [ -f "$src" ] || return 1
  sudo cp -f "$src" "/etc/systemd/system/$base"
  echo "  [+] $base"
  return 0
}

units_installed=0
if [ -d "$SERVICES_DIR" ]; then
  shopt -s nullglob
  for u in "$SERVICES_DIR"/*.service "$SERVICES_DIR"/*.timer; do
    install_unit_if_present "$u" && units_installed=1 || true
  done
  shopt -u nullglob
fi

# If repo doesn’t have units, write sane defaults
if [ "$units_installed" -eq 0 ]; then
  warn "No service files found in $SERVICES_DIR; writing defaults."
  sudo tee /etc/systemd/system/camera-heartbeat.service >/dev/null <<EOF
[Unit]
Description=Camera Heartbeat Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
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
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/src/camera_node.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
fi

sudo systemctl daemon-reload
for s in "${SERVICES[@]}"; do
  sudo systemctl enable --now "$s" || true
done
ok "Services enabled."

# 9) Optional hostname change
if [ -n "${NEW_HOSTNAME:-}" ]; then
  say "Setting hostname to $NEW_HOSTNAME"
  CURRENT_HN="$(hostname)"
  if [ "$CURRENT_HN" != "$NEW_HOSTNAME" ]; then
    echo "$NEW_HOSTNAME" | sudo tee /etc/hostname >/dev/null
    sudo hostnamectl set-hostname "$NEW_HOSTNAME"
    # Update /etc/hosts mapping
    sudo sed -i "s/127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts || true
    ok "Hostname set. Reboot recommended."
  else
    ok "Hostname already $NEW_HOSTNAME"
  fi
fi

# 10) Optional static IP (dhcpcd)
if [ "${APPLY_STATIC_IP:-0}" = "1" ] && [ -n "${STATIC_CIDR:-}" ] && [ -n "${ROUTER_IP:-}" ]; then
  say "Configuring static IP via dhcpcd for $IFACE"
  sudo install -m 644 -T /etc/dhcpcd.conf "/etc/dhcpcd.conf.backup-$(date +%Y%m%d%H%M%S)" || true
  sudo sed -i '/^# CAMNODE static IP START$/,/# CAMNODE static IP END$/{d}' /etc/dhcpcd.conf
  sudo tee -a /etc/dhcpcd.conf >/dev/null <<EOF

# CAMNODE static IP START
interface $IFACE
static ip_address=$STATIC_CIDR
static routers=$ROUTER_IP
$( [ -n "$DNS_LIST" ] && echo "static domain_name_servers=${DNS_LIST// /}" )
# CAMNODE static IP END
EOF
  ok "Static IP block written to /etc/dhcpcd.conf (restart dhcpcd or reboot to apply)."
fi

# 11) Optional SSH key regeneration
if [ "${REGEN_SSH_KEYS:-0}" = "1" ]; then
  say "Regenerating SSH keys (ed25519)..."
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -N "" -C "$USER@$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)" <<< y >/dev/null 2>&1 || true
  chmod 600 "$HOME/.ssh/id_ed25519"
  chmod 644 "$HOME/.ssh/id_ed25519.pub"
  ok "New SSH key generated at ~/.ssh/id_ed25519.pub"
fi

# 12) Health summary
echo
say "== Done =="
for s in "${SERVICES[@]}"; do
  printf "  - %s: " "$s"; systemctl is-active "$s" || true
done
echo
echo "Config: $INSTALL_DIR/config.yaml  (edit node_id/hub_url/token as needed)"
echo "Logs:"
echo "  journalctl -u camera-node -n 80 --no-pager"
echo "  journalctl -u camera-heartbeat -n 80 --no-pager"
echo
echo "Tips:"
echo "  - Set NEW_NODE_ID and NEW_HOSTNAME when cloning a new camera (cam02, cam03, ...)"
echo "  - To apply static IP immediately: sudo systemctl restart dhcpcd"
echo "  - If you changed hostname/I2C groups just now, consider: sudo reboot"
