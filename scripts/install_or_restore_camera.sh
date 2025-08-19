#!/usr/bin/env bash
# === SAVE AS: ~/projects/video-capture-node/scripts/install_or_restore_camera.sh ===
set -euo pipefail

# =========
# Defaults (override with env or flags)
# =========
REPO_SSH="${REPO_SSH:-git@github.com:input86/video-capture-node.git}"
REPO_DIR="${REPO_DIR:-$HOME/projects/video-capture-node}"
RUNTIME_DIR="${RUNTIME_DIR:-/home/pi/camera_node}"               # live target
REPO_CAMERA_DIR="${REPO_CAMERA_DIR:-camera_runtime}"             # source in repo
SERVICES_DIR="${SERVICES_DIR:-services/camera}"                  # systemd unit sources in repo
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"                     # system python
VENVP="${VENVP:-$RUNTIME_DIR/venv}"                              # venv path

# Camera identity overrides (optional; used if building/patching config)
NODE_ID="${NODE_ID:-}"            # e.g., cam02
HUB_URL="${HUB_URL:-}"            # e.g., http://192.168.0.150:8080
AUTH_TOKEN="${AUTH_TOKEN:-}"      # e.g., token for cam02

# Git checkout selection
FROM_TAG="${FROM_TAG:-}"          # e.g., cam-cam01-20250819T...
FROM_COMMIT="${FROM_COMMIT:-}"    # explicit commit SHA
USE_MAIN="${USE_MAIN:-true}"      # if true and no tag/commit provided, use main

# Behavior flags
PRESERVE_CONFIG="${PRESERVE_CONFIG:-true}"  # keep existing config.yaml if present
START_ENABLE="${START_ENABLE:-true}"        # systemctl enable --now
LABEL="${LABEL:-}"                          # optional label for logs

# =========
# Helpers
# =========
log() { echo "[i] $*"; }
warn() { echo "[!] $*" >&2; }
die() { echo "[x] $*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [--tag TAG | --commit SHA | --main]
Options/env:
  --tag TAG         (or FROM_TAG=...)   Checkout a specific tag for restore/install
  --commit SHA      (or FROM_COMMIT=...) Checkout a specific commit
  --main            (or USE_MAIN=true)  Use origin/main (default if nothing else given)

  --node-id ID      (or NODE_ID=...)    Override node_id in config if creating new config
  --hub-url URL     (or HUB_URL=...)    Override hub_url in new config
  --auth-token TOK  (or AUTH_TOKEN=...) Override auth_token in new config

  --preserve-config=true|false  (default true)
  --start-enable=true|false     (default true)
  --label TEXT                  (optional; just for logs)

Examples:
  # Fresh cam02 from cam01-verified tag, reusing per-host config if present
  NODE_ID=cam02 HUB_URL=http://192.168.0.150:8080 AUTH_TOKEN=YOURTOKEN \\
    $(basename "$0") --tag cam-cam01-20250819T123000Z-clean-production-ready

  # Restore from main (latest):
  $(basename "$0") --main
EOF
}

# Parse flags (simple)
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) FROM_TAG="${2:-}"; shift 2;;
    --commit) FROM_COMMIT="${2:-}"; shift 2;;
    --main) USE_MAIN="true"; shift;;
    --node-id) NODE_ID="${2:-}"; shift 2;;
    --hub-url) HUB_URL="${2:-}"; shift 2;;
    --auth-token) AUTH_TOKEN="${2:-}"; shift 2;;
    --preserve-config) PRESERVE_CONFIG="${2:-true}"; shift 2;;
    --start-enable) START_ENABLE="${2:-true}"; shift 2;;
    --label) LABEL="${2:-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) die "Unknown arg: $1 (try --help)";;
  esac
done

# =========
# Pre-flight
# =========
need_cmd git
need_cmd rsync
need_cmd sudo
need_cmd awk
need_cmd sed
need_cmd $PYTHON_BIN
need_cmd systemctl

DATE_HUMAN="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
HOSTTAG="$(hostname | tr -c '[:alnum:]' '-')"
log "Camera install/restore start @ $DATE_HUMAN (host=$HOSTTAG label='${LABEL:-}')"

# =========
# OS deps (best-effort & idempotent)
# =========
log "Ensuring OS packages (best-effort)..."
sudo apt-get update -y || true
sudo apt-get install -y --no-install-recommends \
  git rsync python3-venv python3-pip \
  i2c-tools python3-pil \
  python3-libcamera python3-kms++ \
  python3-numpy python3-yaml \
  libatlas-base-dev \
  || true

# Enable I2C if not already
if ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt 2>/dev/null; then
  log "Enabling I2C in /boot/config.txt"
  echo "dtparam=i2c_arm=on" | sudo tee -a /boot/config.txt >/dev/null || true
fi
sudo adduser pi i2c >/dev/null 2>&1 || true

# =========
# Repo checkout/update
# =========
if [ ! -d "$REPO_DIR/.git" ]; then
  log "Cloning repo into $REPO_DIR"
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch origin --tags --prune --prune-tags || true

if [ -n "$FROM_TAG" ]; then
  log "Checking out tag: $FROM_TAG"
  git checkout -f "$FROM_TAG"
elif [ -n "$FROM_COMMIT" ]; then
  log "Checking out commit: $FROM_COMMIT"
  git checkout -f "$FROM_COMMIT"
elif [ "$USE_MAIN" = "true" ]; then
  log "Checking out main"
  git checkout -f main
  git pull --rebase origin main || true
else
  die "No source specified (use --tag, --commit, or --main)."
fi

# =========
# Sync code to runtime
# =========
mkdir -p "$RUNTIME_DIR"
EXCLUDES=(
  ".git/" "venv/" ".venv/" "__pycache__/" ".mypy_cache/" ".pytest_cache/" "*.pyc"
  ".env" "*.env"
)
RSYNC_ARGS=(-a --delete)
for pat in "${EXCLUDES[@]}"; do RSYNC_ARGS+=("--exclude=$pat"); done

if [ ! -d "$REPO_CAMERA_DIR" ]; then
  die "Repo source directory '$REPO_CAMERA_DIR' not found. Expected camera runtime mirror in repo."
fi

log "Syncing $REPO_CAMERA_DIR -> $RUNTIME_DIR"
rsync "${RSYNC_ARGS[@]}" "$REPO_CAMERA_DIR/" "$RUNTIME_DIR/"

sudo chown -R pi:pi "$RUNTIME_DIR"

# =========
# Python venv & deps
# =========
if [ ! -d "$VENVP" ]; then
  log "Creating venv at $VENVP"
  $PYTHON_BIN -m venv "$VENVP"
fi
# shellcheck disable=SC1090
source "$VENVP/bin/activate"

if [ -f "$RUNTIME_DIR/requirements.txt" ]; then
  log "Installing Python deps from requirements.txt"
  pip install --upgrade pip wheel
  pip install -r "$RUNTIME_DIR/requirements.txt"
else
  log "No requirements.txt found; skipping pip install"
fi

# =========
# Config management
# =========
CFG="$RUNTIME_DIR/config.yaml"
if [ -f "$CFG" ] && [ "$PRESERVE_CONFIG" = "true" ]; then
  log "Existing config.yaml found; preserving."
else
  log "Building new config.yaml"
  # Try to detect prior node_id from repo snapshot (optional)
  PREV_NODE_ID="${NODE_ID:-}"
  if [ -z "$PREV_NODE_ID" ] && [ -f "$REPO_CAMERA_DIR/config.yaml" ]; then
    PREV_NODE_ID="$(awk -F: '/^[[:space:]]*node_id[[:space:]]*:/ {gsub(/ /,"",$2); print $2; exit}' "$REPO_CAMERA_DIR/config.yaml" 2>/dev/null || true)"
  fi

  : "${NODE_ID:=${PREV_NODE_ID:-camXX}}"
  : "${HUB_URL:=http://192.168.0.150:8080}"
  : "${AUTH_TOKEN:=CHANGE_ME_TOKEN}"

  cat > "$CFG" <<EOF
node_id: ${NODE_ID}
hub_url: ${HUB_URL}
auth_token: ${AUTH_TOKEN}

recording:
  resolution: "1920x1080"
  framerate: 30
  duration_s: 5

sensor:
  threshold_mm: 500
  debounce_ms: 800
EOF
  log "Wrote new config.yaml with node_id=${NODE_ID}"
fi

# =========
# Systemd services
# =========
if [ -d "$SERVICES_DIR" ]; then
  log "Installing/updating systemd service units from $SERVICES_DIR"
  for unit in "$SERVICES_DIR"/*.service; do
    [ -e "$unit" ] || continue
    svc="$(basename "$unit")"
    sudo cp -f "$unit" "/etc/systemd/system/$svc"
  done
  sudo systemctl daemon-reload
else
  warn "No services directory at $SERVICES_DIR; skipping unit install."
fi

# =========
# Enable/start services
# =========
if [ "$START_ENABLE" = "true" ]; then
  log "Enabling/starting camera-* services"
  # Enable and start any camera-* units we have
  mapfile -t cam_svcs < <(ls /etc/systemd/system/camera-*.service 2>/dev/null | xargs -n1 -r basename | sed 's/\.service$//')
  for s in "${cam_svcs[@]:-}"; do
    sudo systemctl enable --now "$s" || warn "Failed to start $s"
  done
fi

# =========
# Quick diagnostics
# =========
log "Diagnostics:"
if [ -x "$VENVP/bin/python" ]; then
  "$VENVP/bin/python" - <<'PY'
import sys
print("Python:", sys.version)
try:
  import yaml; print("PyYAML OK")
except Exception as e:
  print("PyYAML MISSING:", e)
try:
  import picamera2; print("Picamera2 OK")
except Exception as e:
  print("Picamera2 issue:", e)
try:
  import board, busio
  from adafruit_vl53l0x import VL53L0X
  print("Blinka + VL53L0X OK (import)")
except Exception as e:
  print("Blinka/VL53L0X import issue:", e)
PY
fi

log "Active camera-* services:"
systemctl list-units --type=service --no-pager | grep -E '^camera-' || true

echo "[✓] Install/restore complete for $RUNTIME_DIR"
