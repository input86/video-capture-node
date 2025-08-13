# === SAVE AS: ~/backup_hub_to_github.sh ===
#!/usr/bin/env bash
set -euo pipefail

# ---------------------------
# Settings (edit if needed)
# ---------------------------
REPO_SSH="git@github.com:input86/video-capture-node.git"
REPO_DIR="$HOME/video-capture-node"

# Runtime folders
HUB_RUNTIME="/home/pi/hub_server"
WEBUI_RUNTIME="/home/pi/hub_web_admin"

# Data / DB
DATA_DIR="/home/pi/data"
HUB_DB="$DATA_DIR/hub.db"

# Service names from your inventory
HUB_SERVICES=(hub-api hub-heartbeat tft-ui)
WEBUI_SERVICES=(hub-web-admin hub-web-thumbs hub-web-thumbs.timer)

# Nginx config locations
NGINX_SITE_AVAIL="/etc/nginx/sites-available/hub_web_admin"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/hub_web_admin"
NGINX_HTPASSWD="/etc/nginx/.htpasswd"   # secret (skip by default)

# Env file for WebUI services (from units)
WEBUI_ENVFILE="/etc/default/hub-web-admin"  # treat as secret-ish

# Secrets handling
# If true, .env files and .htpasswd will NOT be copied into the repo.
EXCLUDE_DOTENV=true
EXCLUDE_HTPASSWD=true

# Commit/tag metadata
DATE_HUMAN="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
DATE_SAFE="$(date -u +'%Y%m%dT%H%M%SZ')"
HOSTTAG="$(hostname | tr -c '[:alnum:]' '-')"
COMMIT_MSG="Working State ${DATE_HUMAN}: hub + webui backup on $(hostname)"
TAG_BASE="working-${DATE_SAFE%-*}-hub"
TAG_NAME="$TAG_BASE"

echo "[i] Backup started at $DATE_HUMAN"

# ---------------------------
# Git identity & safety knobs
# ---------------------------
if ! git config --global user.email >/dev/null; then
  git config --global user.email "pi@$(hostname -f || hostname)"
fi
if ! git config --global user.name >/dev/null; then
  git config --global user.name  "Pi Backup ($(hostname))"
fi
git config --global pull.rebase true
git config --global rebase.autostash true
git config --global fetch.prune true
git config --global fetch.pruneTags true

# ---------------------------
# Clone if needed
# ---------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[i] Cloning repo into $REPO_DIR"
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"

# ---------------------------
# .gitignore hardening (idempotent)
# ---------------------------
if ! grep -q "Python junk" .gitignore 2>/dev/null; then
  cat >> .gitignore <<'EOF'
# Python junk
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/

# Virtual envs
venv/
**/venv/
.venv/
**/.venv/

# Node/JS junk
node_modules/
dist/
build/
.next/

# Media & local state (clips are not stored in repo)
*.mp4

# OS/editor
.DS_Store
Thumbs.db

# Secrets (do not commit)
.env
*.env
.secrets/
secrets/
EOF
  git add .gitignore || true
  git commit -m "gitignore: caches/venv/node_modules/media/secrets (idempotent)" || true
fi

# Avoid fetch clobber warnings if a local tag exists
git tag -d "$TAG_BASE" 2>/dev/null || true

echo "[i] Pulling latest from origin..."
git fetch origin --tags --prune --prune-tags
git checkout main || git checkout -b main
git pull --rebase origin main || true

# ---------------------------
# Ensure repo layout
# ---------------------------
mkdir -p hub_server web_ui services/hub services/webui nginx backups/hub_db backups/logs/"$DATE_SAFE" secrets

# ---------------------------
# Common rsync excludes
# ---------------------------
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
)
if [ "${EXCLUDE_DOTENV}" = true ]; then
  RSYNC_EXCLUDES+=( "--exclude" ".env" "--exclude" "*.env" )
fi

# ---------------------------
# Sync hub_server runtime
# ---------------------------
if [ -d "$HUB_RUNTIME" ]; then
  echo "[i] Syncing hub_server runtime into repo..."
  rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$HUB_RUNTIME/" "hub_server/"
else
  echo "[!] HUB_RUNTIME not found at $HUB_RUNTIME (skipping hub_server sync)"
fi

# ---------------------------
# Sync web_ui runtime
# ---------------------------
if [ -d "$WEBUI_RUNTIME" ]; then
  echo "[i] Syncing web_ui runtime into repo..."
  rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$WEBUI_RUNTIME/" "web_ui/"
else
  echo "[!] WEBUI_RUNTIME not found at $WEBUI_RUNTIME (skipping web_ui sync)"
fi

# ---------------------------
# Save systemd service files
# ---------------------------
save_service_unit() {
  local svc="$1"
  local dest_dir="$2"
  local unit_path="/etc/systemd/system/${svc}.service"

  if [ -f "$unit_path" ]; then
    sudo cp -f "$unit_path" "$dest_dir/${svc}.service" 2>/dev/null || true
    return 0
  fi

  # Fallback: systemctl cat
  if systemctl cat "$svc" >/dev/null 2>&1; then
    systemctl cat "$svc" | sudo tee "$dest_dir/${svc}.service" >/dev/null || true
    return 0
  fi

  return 1
}

echo "[i] Saving systemd service files (hub)..."
for svc in "${HUB_SERVICES[@]}"; do
  save_service_unit "$svc" "services/hub" || echo "[i] (hub) service not found: $svc"
done

echo "[i] Saving systemd service files (webui + timer)..."
for svc in "${WEBUI_SERVICES[@]}"; do
  save_service_unit "$svc" "services/webui" || echo "[i] (webui) service not found: $svc"
done

# ---------------------------
# SQLite DB safe backup
# ---------------------------
if [ -f "$HUB_DB" ]; then
  echo "[i] Backing up hub.db using sqlite3 .backup..."
  TMP_DB="/tmp/hub-$DATE_SAFE.db"
  sqlite3 "$HUB_DB" ".backup '$TMP_DB'"
  mv -f "$TMP_DB" "backups/hub_db/hub-$DATE_SAFE.db"
else
  echo "[!] hub.db not found at $HUB_DB (skipping DB backup)"
fi

# ---------------------------
# Capture recent journal logs
# ---------------------------
echo "[i] Capturing recent journal logs..."
capture_logs() {
  local svc="$1"
  local out="$2"
  sudo journalctl -u "$svc" -n 400 --no-pager > "$out" 2>/dev/null || true
}
for svc in "${HUB_SERVICES[@]}"; do
  capture_logs "$svc" "backups/logs/$DATE_SAFE/${svc}.log"
done
for svc in "${WEBUI_SERVICES[@]}"; do
  # Some entries are .timer; journalctl accepts them too
  capture_logs "$svc" "backups/logs/$DATE_SAFE/${svc}.log"
done

# ---------------------------
# Nginx site + auth (optional)
# ---------------------------
if [ -f "$NGINX_SITE_AVAIL" ]; then
  echo "[i] Saving nginx site: $NGINX_SITE_AVAIL"
  sudo cp -f "$NGINX_SITE_AVAIL" "nginx/hub_web_admin.site" 2>/dev/null || true
  # Also record the symlink target name for clarity
  echo "$NGINX_SITE_LINK -> $NGINX_SITE_AVAIL" > "nginx/hub_web_admin.symlink.txt"
else
  echo "[i] Nginx site not found at $NGINX_SITE_AVAIL (skipping)"
fi

# Optional: .htpasswd (SECRET). Copy to /secrets (gitignored) unless you flip EXCLUDE_HTPASSWD=false
if [ -f "$NGINX_HTPASSWD" ]; then
  if [ "${EXCLUDE_HTPASSWD}" = true ]; then
    echo "[i] Skipping nginx .htpasswd copy (EXCLUDE_HTPASSWD=true)."
  else
    echo "[i] Copying nginx .htpasswd into secrets/ (ensure repo stays private!)"
    sudo cp -f "$NGINX_HTPASSWD" "secrets/nginx.htpasswd" 2>/dev/null || true
  fi
fi

# ---------------------------
# WebUI env file (/etc/default/hub-web-admin)
# ---------------------------
if [ -f "$WEBUI_ENVFILE" ]; then
  if [ "${EXCLUDE_DOTENV}" = true ]; then
    echo "[i] Skipping $WEBUI_ENVFILE copy (EXCLUDE_DOTENV=true)."
  else
    echo "[i] Copying $WEBUI_ENVFILE into secrets/"
    sudo cp -f "$WEBUI_ENVFILE" "secrets/hub-web-admin.env" 2>/dev/null || true
  fi
fi

# ---------------------------
# Manifest
# ---------------------------
{
  echo "Manifest UTC: $DATE_HUMAN"
  echo "Host: $(hostname)"
  echo "Kernel: $(uname -a)"
  echo "Data dir: $DATA_DIR"
  echo "DB path:  $HUB_DB"
  echo
  echo "Hub services status:"
  for svc in "${HUB_SERVICES[@]}"; do
    printf "  %-24s %s\n" "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)"
  done
  echo
  echo "WebUI services status:"
  for svc in "${WEBUI_SERVICES[@]}"; do
    printf "  %-24s %s\n" "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)"
  done
  echo
  echo "Nginx site available: $NGINX_SITE_AVAIL"
  echo "Nginx site enabled:   $NGINX_SITE_LINK"
  echo "Basic auth file:      $NGINX_HTPASSWD (copied? $([ "${EXCLUDE_HTPASSWD}" = true ] && echo no || echo yes))"
  echo "WebUI env file:       $WEBUI_ENVFILE (copied? $([ "${EXCLUDE_DOTENV}" = true ] && echo no || echo yes))"
} > "backups/logs/$DATE_SAFE/manifest.txt"

# ---------------------------
# Commit & push
# ---------------------------
echo "[i] Committing changes..."
git add hub_server web_ui services/hub services/webui nginx backups/hub_db backups/logs/"$DATE_SAFE" secrets 2>/dev/null || true
git commit -m "$COMMIT_MSG" || echo "[i] Nothing to commit."
git push origin main

# ---------------------------
# Tag handling
# ---------------------------
echo "[i] Handling tag..."
if git ls-remote --tags origin | grep -q "refs/tags/$TAG_BASE$"; then
  TAG_NAME="${TAG_BASE}-${HOSTTAG}-${DATE_SAFE}"
  echo "[i] Remote already has $TAG_BASE. Using unique tag: $TAG_NAME"
else
  echo "[i] Creating base tag: $TAG_NAME"
fi
git tag -a "$TAG_NAME" -m "$COMMIT_MSG" || true
git push origin "$TAG_NAME"

echo "[✓] Backup complete. Tag: $TAG_NAME"
