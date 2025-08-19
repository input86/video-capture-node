# Save into your repo (on the hub):
mkdir -p ~/video-capture-node/scripts
cat > ~/video-capture-node/scripts/restore_hub_from_repo.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/video-capture-node"
RUNTIME_DIR="$HOME/hub_server"
DATA_DIR="$HOME/data"
DB_PATH="$DATA_DIR/hub.db"

echo "[i] Restoring Hub from repo ? runtime"

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

# 1) Sync code into runtime (no venv/__pycache__)
mkdir -p "$RUNTIME_DIR"
rsync -av --delete \
  --exclude ".git/" --exclude "venv/" \
  --exclude "__pycache__/" --exclude "*.pyc" \
  "$REPO_DIR/hub_server/" "$RUNTIME_DIR/"

# 2) Python venv + deps
cd "$RUNTIME_DIR"
python3 -m venv venv || true
source venv/bin/activate
if [ -f requirements.txt ]; then
  pip install -U pip wheel
  pip install -r requirements.txt
else
  # Minimal deps fallback
  pip install flask gunicorn requests pyyaml
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
copy_if "$REPO_DIR/services/hub/hub-api.service"       /etc/systemd/system/hub-api.service
copy_if "$REPO_DIR/services/hub/hub-heartbeat.service" /etc/systemd/system/hub-heartbeat.service
copy_if "$REPO_DIR/services/hub/tft-ui.service"        /etc/systemd/system/tft-ui.service

sudo systemctl daemon-reload

# 4) Ensure data dir + DB exist (create if missing; don’t drop existing)
mkdir -p "$DATA_DIR"
if [ ! -f "$DB_PATH" ]; then
  echo "[i] Creating new hub.db at $DB_PATH"
  sqlite3 "$DB_PATH" <<'SQL'
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  last_seen TEXT,
  status TEXT,
  ip TEXT,
  version TEXT,
  free_space_pct REAL,
  queue_len INTEGER
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT,
  filepath TEXT,
  timestamp TEXT
);
SQL
fi

# 5) Start services
sudo systemctl enable hub-api hub-heartbeat tft-ui --now || true
sleep 2

echo "[i] Checking ports and endpoints…"
ss -tulpn | egrep ':5000|:5050' || true
curl -s http://127.0.0.1:5000/        | head -c 120; echo
curl -s http://127.0.0.1:5050/api/v1/nodes | head -c 200; echo

echo "[?] Hub restore complete."
EOF
chmod +x ~/video-capture-node/scripts/restore_hub_from_repo.sh
