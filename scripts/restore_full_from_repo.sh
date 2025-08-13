# === SAVE AS: ~/video-capture-node/scripts/restore_full_from_repo.sh ===
#!/usr/bin/env bash
set -euo pipefail

# ---------------------------
# Settings (edit if needed)
# ---------------------------
REPO_SSH="git@github.com:input86/video-capture-node.git"
REPO_DIR="$HOME/video-capture-node"

HUB_RUNTIME="$HOME/hub_server"
WEBUI_RUNTIME="$HOME/hub_web_admin"

DATA_DIR="$HOME/data"
DB_PATH="$DATA_DIR/hub.db"

# Ports (for basic health checks)
HUB_PORT=5000          # gunicorn Flask API (hub-api.service)
WEBUI_PROXY_PORT=80    # nginx front
WEBUI_BACKEND_PORT=8080  # gunicorn bind for webui (if applicable)

# Files in repo
HUB_REPO_DIR="$REPO_DIR/hub_server"
WEBUI_REPO_DIR="$REPO_DIR/web_ui"
SERVICES_HUB_DIR="$REPO_DIR/services/hub"
SERVICES_WEBUI_DIR="$REPO_DIR/services/webui"
NGINX_SITE_REPO="$REPO_DIR/nginx/hub_web_admin.site"
NGINX_SYMLINK_NOTE="$REPO_DIR/nginx/hub_web_admin.symlink.txt"
SECRETS_DIR="$REPO_DIR/secrets"

# Optional: initialize DB schema if no backup found or DB missing
init_db_schema() {
  echo "[i] Initializing fresh hub.db schema at $DB_PATH"
  mkdir -p "$(dirname "$DB_PATH")"
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
}

# ---------------------------
# 0) Ensure repo present and updated on main
# ---------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[i] Cloning repo..."
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --tags --prune --prune-tags
git checkout main || git checkout -b main
git pull --rebase || true

# ---------------------------
# 1) Install base packages (idempotent)
# ---------------------------
echo "[1/7] Installing system packages..."
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip sqlite3 git curl \
  nginx python3-dev build-essential libatlas-base-dev

# ---------------------------
# 2) Restore code: hub + webui
# ---------------------------
echo "[2/7] Restoring code to runtime..."
mkdir -p "$HUB_RUNTIME" "$WEBUI_RUNTIME"

# Common rsync excludes
RSYNC_EXCLUDES=(
  "--exclude" ".git/"
  "--exclude" "venv/"
  "--exclude" ".venv/"
  "--exclude" "__pycache__/"
  "--exclude" ".mypy_cache/"
  "--exclude" ".pytest_cache/"
  "--exclude" "*.pyc"
  "--exclude" "node_modules/"
  "--exclude" "dist/"
  "--exclude" "build/"
  "--exclude" ".next/"
  "--exclude" ".env"
  "--exclude" "*.env"
)

if [ -d "$HUB_REPO_DIR" ]; then
  rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$HUB_REPO_DIR/" "$HUB_RUNTIME/"
else
  echo "[!] Hub repo dir not found: $HUB_REPO_DIR"
fi

if [ -d "$WEBUI_REPO_DIR" ]; then
  rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$WEBUI_REPO_DIR/" "$WEBUI_RUNTIME/"
else
  echo "[!] WebUI repo dir not found: $WEBUI_REPO_DIR"
fi

# ---------------------------
# 3) Python venvs + deps (hub + webui)
# ---------------------------
echo "[3/7] Recreating Python venvs and installing deps..."

setup_venv() {
  local app_dir="$1"
  [ -d "$app_dir" ] || return 0
  python3 -m venv "$app_dir/venv" || true
  source "$app_dir/venv/bin/activate"
  pip install --upgrade pip wheel
  if [ -f "$app_dir/requirements.txt" ]; then
    pip install -r "$app_dir/requirements.txt"
  else
    # Minimal safe defaults for Flask/Gunicorn apps
    pip install flask gunicorn pyyaml requests
  fi
  deactivate
}

setup_venv "$HUB_RUNTIME"
setup_venv "$WEBUI_RUNTIME"

# If WebUI has a frontend build (optional)
if [ -f "$WEBUI_RUNTIME/package.json" ]; then
  echo "[i] Detected web_ui package.json; installing node deps..."
  if ! command -v npm >/dev/null 2>&1; then
    echo "[!] npm not found. Install Node.js if you need to build the frontend."
  else
    (cd "$WEBUI_RUNTIME" && npm ci && npm run build) || echo "[i] Skipping frontend build (optional)."
  fi
fi

# ---------------------------
# 4) Restore DB from latest backup (or init fresh)
# ---------------------------
echo "[4/7] Restoring database..."
mkdir -p "$DATA_DIR"
LATEST_DB="$(ls -1t "$REPO_DIR"/backups/hub_db/hub-*.db 2>/dev/null | head -n1 || true)"

if [ -n "${LATEST_DB:-}" ] && [ -f "$LATEST_DB" ]; then
  echo "[i] Latest DB snapshot: $(basename "$LATEST_DB")"
  cp -f "$LATEST_DB" "$DB_PATH"
else
  echo "[i] No DB snapshot found in $REPO_DIR/backups/hub_db/. Creating fresh DB."
  init_db_schema
fi
echo "[i] DB at: $DB_PATH"

# ---------------------------
# 5) Systemd services/timers install & enable
# ---------------------------
echo "[5/7] Installing systemd services/timers from repo..."

install_units_from_dir() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  shopt -s nullglob
  for unit in "$dir"/*.service "$dir"/*.timer; do
    [ -e "$unit" ] || continue
    local base="$(basename "$unit")"
    sudo cp -f "$unit" "/etc/systemd/system/$base"
    echo "  [+] Installed $base"
  done
  shopt -u nullglob
}

install_units_from_dir "$SERVICES_HUB_DIR"
install_units_from_dir "$SERVICES_WEBUI_DIR"

sudo systemctl daemon-reload

# Enable/start all installed units we just copied
enable_start_units_from_dir() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  shopt -s nullglob
  for unit in "$dir"/*.service "$dir"/*.timer; do
    [ -e "$unit" ] || continue
    local base="$(basename "$unit")"
    sudo systemctl enable --now "$base" || true
  done
  shopt -u nullglob
}

enable_start_units_from_dir "$SERVICES_HUB_DIR"
enable_start_units_from_dir "$SERVICES_WEBUI_DIR"

# ---------------------------
# 6) Nginx site restore (+ optional secrets)
# ---------------------------
echo "[6/7] Restoring nginx site..."
if [ -f "$NGINX_SITE_REPO" ]; then
  sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
  sudo cp -f "$NGINX_SITE_REPO" /etc/nginx/sites-available/hub_web_admin
  sudo ln -sf /etc/nginx/sites-available/hub_web_admin /etc/nginx/sites-enabled/hub_web_admin
  # Optional secrets: htpasswd
  if [ -f "$SECRETS_DIR/nginx.htpasswd" ]; then
    echo "[i] Installing nginx htpasswd from secrets/"
    sudo cp -f "$SECRETS_DIR/nginx.htpasswd" /etc/nginx/.htpasswd
  fi
  sudo nginx -t && sudo systemctl restart nginx || echo "[!] nginx test failed; review /etc/nginx/sites-available/hub_web_admin"
else
  echo "[i] No nginx site file found at $NGINX_SITE_REPO (skipping nginx restore)"
fi

# Optional: WebUI environment file (if you keep it in secrets)
if [ -f "$SECRETS_DIR/hub-web-admin.env" ]; then
  echo "[i] Installing /etc/default/hub-web-admin from secrets/"
  sudo cp -f "$SECRETS_DIR/hub-web-admin.env" /etc/default/hub-web-admin
  sudo systemctl restart hub-web-admin || true
fi

# ---------------------------
# 7) Health checks & summary
# ---------------------------
echo "[7/7] Health checks..."
sleep 2
echo "[i] systemd statuses (short):"
systemctl is-active hub-api 2>/dev/null || true
systemctl is-active tft-ui 2>/dev/null || true
systemctl is-active hub-web-admin 2>/dev/null || true
systemctl is-active hub-web-thumbs 2>/dev/null || true
systemctl is-active hub-web-thumbs.timer 2>/dev/null || true

echo
echo "[i] Listening ports (filtered):"
sudo ss -tulpn | grep -E ":($HUB_PORT|$WEBUI_BACKEND_PORT|80|443)\b" || true

echo
echo "[i] Curl checks:"
curl -s "http://127.0.0.1:${HUB_PORT}/" | head -c 200; echo || true
curl -s "http://127.0.0.1:${WEBUI_PROXY_PORT}/" | head -c 200; echo || true

echo
echo "[✓] Restore complete."
echo "    - Code: $HUB_RUNTIME , $WEBUI_RUNTIME"
echo "    - DB:   $DB_PATH (source: ${LATEST_DB:-fresh})"
echo "    - Nginx: $(if [ -f "$NGINX_SITE_REPO" ]; then echo enabled; else echo skipped; fi)"
echo "    - Services: installed from $SERVICES_HUB_DIR and $SERVICES_WEBUI_DIR"
