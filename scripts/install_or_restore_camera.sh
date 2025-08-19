#!/usr/bin/env bash
# === install_or_restore_camera.sh ===
set -euo pipefail

# ===========
# Parameters
# ===========
# Usage:
#   NODE_ID=cam02 HUB_URL=http://192.168.0.150:8080 AUTH_TOKEN=... \
#   ./install_or_restore_camera.sh --tag <TAG>
#
#   ./install_or_restore_camera.sh --main
#   ./install_or_restore_camera.sh --commit <SHA>
#
# Env:
#   NODE_ID (required for fresh install or when PRESERVE_CONFIG=false)
#   HUB_URL (required for fresh install or when PRESERVE_CONFIG=false)
#   AUTH_TOKEN (required for fresh install or when PRESERVE_CONFIG=false)
#   PRESERVE_CONFIG=true|false (default: true)
#   START_ENABLE=true|false (default: true)

MODE="${1:-}"
ARG="${2:-}"

PRESERVE_CONFIG="${PRESERVE_CONFIG:-true}"
START_ENABLE="${START_ENABLE:-true}"

REPO_DIR="${REPO_DIR:-$HOME/projects/video-capture-node}"
RUNTIME_DIR="${RUNTIME_DIR:-/home/pi/camera_node}"
RUNTIME_SRC_DIR="${RUNTIME_SRC_DIR:-camera_runtime}"

# Services expected
CAMERA_SVCS=(camera-node camera-heartbeat)

# ===========
# Helpers
# ===========
die() { echo "[ERROR] $*" >&2; exit 1; }
info() { echo "[i] $*"; }
ok()   { echo "[✓] $*"; }

need_root() { [ "$(id -u)" -eq 0 ] || die "Run as root"; }

# ===========
# Preflight
# ===========
[ -d "$REPO_DIR/.git" ] || die "Repo not found at $REPO_DIR. Clone it first."
cd "$REPO_DIR"

if [ -z "$MODE" ]; then
  die "Specify one of: --tag <TAG> | --commit <SHA> | --main"
fi

# Use main/commit/tag
case "$MODE" in
  --main)
    info "Checking out latest main..."
    git fetch origin --tags --prune --prune-tags
    git checkout main || git checkout -b main
    git pull --rebase origin main || true
    ;;
  --commit)
    [ -n "$ARG" ] || die "--commit requires a SHA"
    git fetch origin --tags --prune --prune-tags
    info "Checking out commit $ARG ..."
    git checkout "$ARG"
    ;;
  --tag)
    [ -n "$ARG" ] || die "--tag requires a tag name"
    git fetch --tags --prune --prune-tags
    git checkout "tags/$ARG" -B "restore-$ARG"
    ;;
  *)
    die "Unknown mode: $MODE"
    ;;
esac

# ===========
# APT deps
# ===========
info "Installing apt dependencies..."
sudo apt-get update
sudo apt-get install -y \
  ffmpeg \
  python3-picamera2 python3-libcamera python3-kms++ python3-numpy \
  python3-rpi.gpio python3-libgpiod \
  i2c-tools libatlas-base-dev \
  python3-dev build-essential

# ===========
# Enable I2C, add user to i2c
# ===========
if ! grep -q '^dtparam=i2c_arm=on' /boot/config.txt 2>/dev/null; then
  info "Enabling I2C in /boot/config.txt ..."
  echo 'dtparam=i2c_arm=on' | sudo tee -a /boot/config.txt >/dev/null
fi
sudo adduser pi i2c >/dev/null 2>&1 || true

# ===========
# Sync runtime from repo
# ===========
mkdir -p "$RUNTIME_DIR"
info "Syncing ${RUNTIME_SRC_DIR}/ -> ${RUNTIME_DIR}/ ..."
rsync -av --delete \
  --exclude ".git/" \
  --exclude "venv/" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude ".mypy_cache/" \
  --exclude ".pytest_cache/" \
  --exclude "*.pyc" \
  --exclude ".env" --exclude "*.env" \
  "${RUNTIME_SRC_DIR}/" "${RUNTIME_DIR}/"

# ===========
# Venv with system site-packages (so picamera2 is visible)
# ===========
info "Creating Python venv with system site-packages..."
if [ -d "${RUNTIME_DIR}/venv" ]; then
  mv "${RUNTIME_DIR}/venv" "${RUNTIME_DIR}/venv.bak_$(date +%s)" || true
fi
python3 -m venv --system-site-packages "${RUNTIME_DIR}/venv"

# Activate venv and install Python deps
# (We include GPIO backends and Blinka/VL53L0X stack)
# shellcheck disable=SC1091
source "${RUNTIME_DIR}/venv/bin/activate"
pip install --upgrade pip wheel
pip install \
  pyyaml gpiozero requests \
  adafruit-blinka adafruit-circuitpython-vl53l0x \
  RPi.GPIO rpi-lgpio

# Fallback hook: ensure /usr/lib/python3/dist-packages is in venv sys.path
python - <<'PY' || true
import sys, pathlib
if '/usr/lib/python3/dist-packages' not in sys.path:
    for p in sys.path:
        if p.endswith('site-packages'):
            hook = pathlib.Path(p)/'_sys_path_additions.pth'
            hook.write_text('/usr/lib/python3/dist-packages\n')
            print('[i] Wrote path hook:', hook)
            break
PY

# Verify picamera2 import
python - <<'PY'
try:
    import picamera2
    import sys
    print('[i] picamera2 import OK from:', picamera2.__file__)
except Exception as e:
    print('[WARN] picamera2 import failed in venv:', e)
    raise SystemExit(0)
PY

deactivate || true

# ===========
# Config file
# ===========
CFG="${RUNTIME_DIR}/config.yaml"
if [ ! -f "$CFG" ] || [ "${PRESERVE_CONFIG}" = "false" ]; then
  : "${NODE_ID:?NODE_ID is required when creating config}"
  : "${HUB_URL:?HUB_URL is required when creating config}"
  : "${AUTH_TOKEN:?AUTH_TOKEN is required when creating config}"
  info "Writing ${CFG} ..."
  cat > "$CFG" <<EOF
hub_url: ${HUB_URL}
node_id: ${NODE_ID}
auth_token: ${AUTH_TOKEN}
profile: storage_saver_720p30
bitrate_kbps: 10000
rotation: 0
sensor:
  threshold_mm: 1000
  debounce_ms: 200
  xshut_gpio: 4
recording:
  resolution: 1280x720
  framerate: 30
  duration_s: 6
  bitrate_kbps: 22000
  rotation: 0
storage:
  min_free_percent: 10
heartbeat_interval_sec: 10
EOF
else
  info "Preserving existing ${CFG}"
fi

# ===========
# Systemd units
# ===========
UNIT_DIR="/etc/systemd/system"
SRC_UNIT_DIR_REPO="$REPO_DIR/services/camera"

install_unit() {
  local name="$1"
  local src="$SRC_UNIT_DIR_REPO/${name}.service"
  local dst="$UNIT_DIR/${name}.service"

  if [ -f "$src" ]; then
    info "Installing unit from repo: ${name}.service"
    sudo install -m 0644 "$src" "$dst"
  else
    info "Generating default unit: ${name}.service"
    case "$name" in
      camera-node)
        sudo tee "$dst" >/dev/null <<UNIT
[Unit]
Description=Camera Node Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${RUNTIME_DIR}
ExecStart=${RUNTIME_DIR}/venv/bin/python3 ${RUNTIME_DIR}/src/camera_node.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
        ;;
      camera-heartbeat)
        sudo tee "$dst" >/dev/null <<UNIT
[Unit]
Description=Camera Heartbeat Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${RUNTIME_DIR}
ExecStart=${RUNTIME_DIR}/venv/bin/python ${RUNTIME_DIR}/heartbeat_client.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
        ;;
      *)
        die "Unknown service to generate: $name"
        ;;
    esac
  fi
}

for s in "${CAMERA_SVCS[@]}"; do
  install_unit "$s"
done

sudo systemctl daemon-reload

if [ "${START_ENABLE}" = "true" ]; then
  info "Enabling & starting services..."
  for s in "${CAMERA_SVCS[@]}"; do
    sudo systemctl enable "$s" || true
    sudo systemctl restart "$s" || true
  done
fi

ok "Install/restore complete."
echo
echo "Status summary:"
for s in "${CAMERA_SVCS[@]}"; do
  systemctl is-active "$s" >/dev/null 2>&1 && state="active" || state="inactive"
  printf "  %-18s %s\n" "$s" "$state"
done
echo
echo "If camera-node logs show ffmpeg/picamera2 errors, re-run:"
echo "  sudo journalctl -u camera-node -n 120 --no-pager"
