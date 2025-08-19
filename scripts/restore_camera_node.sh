# Save into your repo (on the camera):
mkdir -p ~/video-capture-node/scripts
cat > ~/video-capture-node/scripts/restore_camera_from_repo.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/video-capture-node"
RUNTIME_DIR="$HOME/camera_node"

echo "[i] Restoring Camera Node from repo ? runtime"

# 0) Ensure repo exists & up-to-date
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[!] Repo not found at $REPO_DIR"
  echo "    git clone git@github.com:input86/video-capture-node.git \"$REPO_DIR\""
  exit 1
fi
cd "$REPO_DIR"
git fetch --tags --prune
git checkout main
git pull --rebase

# 1) Sync code into runtime (no venv/__pycache__/tmp)
mkdir -p "$RUNTIME_DIR"
rsync -av --delete \
  --exclude ".git/" --exclude "venv/" \
  --exclude "__pycache__/" --exclude "*.pyc" \
  --exclude "tmp/" \
  "$REPO_DIR/camera_node/" "$RUNTIME_DIR/"

# 2) Python venv + deps
cd "$RUNTIME_DIR"
python3 -m venv venv || true
source venv/bin/activate
if [ -f requirements.txt ]; then
  pip install -U pip wheel
  pip install -r requirements.txt
else
  # Minimal deps fallback
  pip install requests pyyaml
fi
deactivate

# 3) Install/refresh systemd services from repo if present
copy_if() {
  local src="$1" dst="$2"
  if [ -f "$src" ]; then
    sudo cp -f "$src" "$dst"
    echo "[i] Installed $(basename "$dst")"
  fi
}
copy_if "$REPO_DIR/services/camera/camera-node.service"      /etc/systemd/system/camera-node.service
copy_if "$REPO_DIR/services/camera/camera-heartbeat.service" /etc/systemd/system/camera-heartbeat.service

sudo systemctl daemon-reload

# 4) Ensure basic runtime dirs
mkdir -p "$RUNTIME_DIR/tmp" "$RUNTIME_DIR/queue"

# 5) Start services
sudo systemctl enable camera-node camera-heartbeat --now || true
sleep 2

echo "[i] Tail a few lines to confirm:"
sudo journalctl -u camera-node -n 30 --no-pager || true
sudo journalctl -u camera-heartbeat -n 30 --no-pager || true

echo "[?] Camera restore complete."
EOF
chmod +x ~/video-capture-node/scripts/restore_camera_from_repo.sh
