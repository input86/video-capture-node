#!/usr/bin/env bash
set -euo pipefail

# --- Settings (from inventory) ---
REPO_SSH="git@github.com:input86/video-capture-node.git"
REPO_DIR="$HOME/projects/video-capture-node"

INSTALL_DIR="/home/pi/camera_node"         # live runtime target

SRC_RUNTIME_DIR="$REPO_DIR/camera_runtime"  # preferred source (from backups)
SRC_FALLBACK_DIR="$REPO_DIR/camera_node"    # fallback (project scaffolding)

SERVICES_DIR="$REPO_DIR/services/camera"
SERVICES=(camera-node camera-heartbeat)

# Optional: set SKIP_HW_CHECK=1 to bypass I²C/libcamera health checks
SKIP_HW_CHECK="${SKIP_HW_CHECK:-0}"

# --- Repo present & updated (main) ---
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[i] Cloning repo..."
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --tags --prune --prune-tags
git checkout main || git checkout -b main
git pull --rebase || true

# --- Base OS packages (idempotent) ---
echo "[1/6] Installing OS packages..."
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  python3-libcamera python3-picamera2 libcamera-apps \
  ffmpeg i2c-tools python3-rpi.gpio \
  dhcpcd5 git curl

# Groups & I²C (no reboot required for re-run; reboot may be needed on first time)
sudo usermod -aG video,i2c,gpio "$USER" || true
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_i2c 0 || true
fi

# --- Stop running services before replacing files ---
echo "[2/6] Stopping services (if running)..."
for s in "${SERVICES[@]}"; do
  sudo systemctl stop "$s" 2>/dev/null || true
done

# --- Restore code to runtime ---
echo "[3/6] Restoring camera code to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"

SRC_DIR=""
if [ -d "$SRC_RUNTIME_DIR" ]; then
  SRC_DIR="$SRC_RUNTIME_DIR"
  echo "[i] Using source: $SRC_RUNTIME_DIR"
elif [ -d "$SRC_FALLBACK_DIR" ]; then
  SRC_DIR="$SRC_FALLBACK_DIR"
  echo "[i] Using fallback source: $SRC_FALLBACK_DIR"
else
  echo "[!] No source found at $SRC_RUNTIME_DIR or $SRC_FALLBACK_DIR"
  exit 1
fi

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
sudo chown -R "$USER":"$USER" "$INSTALL_DIR"

# Sanity: required files
need_missing=0
for f in "src/camera_node.py" "heartbeat_client.py" "config.yaml"; do
  if [ ! -f "$INSTALL_DIR/$f" ]; then
    echo "[!] Missing $INSTALL_DIR/$f"
    need_missing=1
  fi
done
if [ $need_missing -ne 0 ]; then
  echo "[!] Missing required files; fix the repo copy then re-run."
  exit 1
fi

# --- Python venv & deps (incl. Blinka + VL53L0X) ---
echo "[4/6] Creating venv & installing Python deps..."
python3 -m venv --system-site-packages "$INSTALL_DIR/venv" || true
# shellcheck disable=SC1091
source "$INSTALL_DIR/venv/bin/activate"
pip install --upgrade pip wheel
if [ -f "$SRC_DIR/requirements.txt" ]; then
  pip install -r "$SRC_DIR/requirements.txt"
else
  pip install \
    requests pyyaml gpiozero adafruit-circuitpython-vl53l0x adafruit-blinka
fi
deactivate

# --- Install services from repo and enable ---
echo "[5/6] Installing systemd units..."
install_unit_if_present() {
  local src="$1"
  local base
  base="$(basename "$src")"
  [ -f "$src" ] || return 0
  sudo cp -f "$src" "/etc/systemd/system/$base"
  echo "  [+] $base"
}

if [ -d "$SERVICES_DIR" ]; then
  shopt -s nullglob
  for u in "$SERVICES_DIR"/*.service "$SERVICES_DIR"/*.timer; do
    install_unit_if_present "$u"
  done
  shopt -u nullglob
else
  echo "[i] No services directory at $SERVICES_DIR (skipping copy)"
fi

sudo systemctl daemon-reload
for s in "${SERVICES[@]}"; do
  sudo systemctl enable --now "$s" || true
done

# --- Optional hardware health checks (guarded) ---
echo "[6/6] Health checks..."
sleep 2
for s in "${SERVICES[@]}"; do
  echo -n "  - $s: "
  systemctl is-active "$s" || true
done

if [ "$SKIP_HW_CHECK" != "1" ]; then
  echo
  echo "[i] Quick I²C and camera check (non-fatal, with timeouts)..."
  # Temporarily stop node to free I²C/camera for probe
  sudo systemctl stop camera-node 2>/dev/null || true
  # Gentle, targeted I²C read (VL53L0X is typically 0x29); timeout prevents hangs
  timeout 5s sudo i2cdetect -y -r 1 0x29 0x29 || echo "(i2c probe skipped/busy)"
  # Camera version check (does not open camera stream)
  timeout 5s libcamera-hello --version 2>/dev/null || echo "(libcamera not probed)"
  # Restart node
  sudo systemctl start camera-node 2>/dev/null || true
fi

echo
echo "[✓] Camera restore complete."
echo "  Runtime: $INSTALL_DIR"
echo "  Services: ${SERVICES[*]}"
echo
echo "Logs:"
echo "  journalctl -u camera-node -n 80 --no-pager"
echo "  journalctl -u camera-heartbeat -n 80 --no-pager"
echo
echo "Tip: set SKIP_HW_CHECK=1 to bypass I²C/camera probe:"
echo "  SKIP_HW_CHECK=1 $0"
