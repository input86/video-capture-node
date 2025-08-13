#!/usr/bin/env bash
set -euo pipefail

# --- Settings (from inventory) ---
REPO_SSH="git@github.com:input86/video-capture-node.git"
REPO_DIR="$HOME/projects/video-capture-node"

CAM_RUNTIME="/home/pi/camera_node"        # live code
CAM_REPO_DST="camera_runtime"             # repo mirror of live runtime

CAM_SERVICES=(camera-node camera-heartbeat)

DATE_HUMAN="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
DATE_SAFE="$(date -u +'%Y%m%dT%H%M%SZ')"
HOSTTAG="$(hostname | tr -c '[:alnum:]' '-')"
COMMIT_MSG="Camera backup ${DATE_HUMAN} on ${HOSTTAG}"
TAG_BASE="cam-backup-${DATE_SAFE%-*}"
TAG_NAME="$TAG_BASE"

echo "[i] Camera backup started at $DATE_HUMAN"

# --- Ensure repo checkout (main only) ---
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[i] Cloning repo into $REPO_DIR"
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --tags --prune --prune-tags
git checkout main || git checkout -b main
git pull --rebase || true

# --- Layout ---
mkdir -p "$CAM_REPO_DST" services/camera backups/logs/"$DATE_SAFE" backups/camera

# --- Mirror runtime → repo (exclude venv/secrets/caches) ---
if [ -d "$CAM_RUNTIME" ]; then
  echo "[i] Syncing $CAM_RUNTIME -> $REPO_DIR/$CAM_REPO_DST ..."
  rsync -av --delete \
    --exclude ".git/" \
    --exclude "venv/" \
    --exclude ".venv/" \
    --exclude "__pycache__/" \
    --exclude ".mypy_cache/" \
    --exclude ".pytest_cache/" \
    --exclude "*.pyc" \
    --exclude ".env" --exclude "*.env" \
    "$CAM_RUNTIME/" "$CAM_REPO_DST/"
else
  echo "[!] Camera runtime not found at $CAM_RUNTIME (skipping code sync)"
fi

# --- Save systemd service units ---
save_unit() {
  local svc="$1"
  local dest="services/camera/${svc}.service"
  local path="/etc/systemd/system/${svc}.service"
  if [ -f "$path" ]; then
    sudo cp -f "$path" "$dest" 2>/dev/null || true
  elif systemctl cat "$svc" >/dev/null 2>&1; then
    systemctl cat "$svc" | sudo tee "$dest" >/dev/null || true
  else
    echo "[i] (missing) $svc"
  fi
}
echo "[i] Saving service files..."
for s in "${CAM_SERVICES[@]}"; do
  save_unit "$s"
done

# --- Logs + pip freeze + config snapshot ---
echo "[i] Capturing logs..."
for s in "${CAM_SERVICES[@]}"; do
  sudo journalctl -u "$s" -n 400 --no-pager > "backups/logs/$DATE_SAFE/${s}.log" 2>/dev/null || true
done

if [ -f "$CAM_RUNTIME/venv/bin/pip" ]; then
  "$CAM_RUNTIME/venv/bin/pip" freeze > "backups/camera/pip-freeze-$DATE_SAFE.txt" || true
fi

# Save current config.yaml safely
if [ -f "$CAM_RUNTIME/config.yaml" ]; then
  cp -f "$CAM_RUNTIME/config.yaml" "backups/camera/config-$DATE_SAFE.yaml"
fi

# Minimal manifest
{
  echo "UTC: $DATE_HUMAN"
  echo "Host: $HOSTTAG"
  echo "Kernel: $(uname -a)"
  echo "Python: $(python3 --version 2>/dev/null)"
  echo
  echo "Runtime: $CAM_RUNTIME"
  echo "Repo dst: $CAM_REPO_DST"
  echo "Services: ${CAM_SERVICES[*]}"
} > "backups/logs/$DATE_SAFE/manifest.txt"

# --- Commit & push (main) ---
git add "$CAM_REPO_DST" services/camera backups/logs/"$DATE_SAFE" backups/camera || true
git commit -m "$COMMIT_MSG" || echo "[i] Nothing to commit."
git push origin main

# --- Tag (optional; non-disruptive) ---
if git ls-remote --tags origin | grep -q "refs/tags/$TAG_BASE$"; then
  TAG_NAME="${TAG_BASE}-${HOSTTAG}-${DATE_SAFE}"
fi
git tag -a "$TAG_NAME" -m "$COMMIT_MSG" || true
git push origin "$TAG_NAME"

echo "[✓] Camera backup complete → $REPO_DIR ($TAG_NAME)"
