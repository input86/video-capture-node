#!/usr/bin/env bash
# === SAVE AS: ~/projects/video-capture-node/scripts/backup_hub_to_github.sh ===
set -euo pipefail

# ---------------------------
# Settings (override via env)
# ---------------------------
REPO_SSH="${REPO_SSH:-git@github.com:input86/video-capture-node.git}"
REPO_DIR="${REPO_DIR:-$HOME/projects/video-capture-node}"

# Runtime folders
HUB_RUNTIME="${HUB_RUNTIME:-/home/pi/hub_server}"
WEBUI_RUNTIME="${WEBUI_RUNTIME:-/home/pi/hub_web_admin}"

# Data / DB
DATA_DIR="${DATA_DIR:-/home/pi/data}"
HUB_DB="${HUB_DB:-$DATA_DIR/hub.db}"

# Service names from your inventory (explicit)
HUB_SERVICES=( ${HUB_SERVICES_OVERRIDE:-hub-api hub-heartbeat tft-ui} )
WEBUI_SERVICES=( ${WEBUI_SERVICES_OVERRIDE:-hub-web-admin hub-web-thumbs hub-web-thumbs.timer} )

# Nginx config locations
NGINX_SITE_AVAIL="${NGINX_SITE_AVAIL:-/etc/nginx/sites-available/hub_web_admin}"
NGINX_SITE_LINK="${NGINX_SITE_LINK:-/etc/nginx/sites-enabled/hub_web_admin}"
NGINX_HTPASSWD="${NGINX_HTPASSWD:-/etc/nginx/.htpasswd}"   # secret (skipped by default)

# Env file for WebUI services (from units)
WEBUI_ENVFILE="${WEBUI_ENVFILE:-/etc/default/hub-web-admin}"  # treat as secret-ish

# Secrets handling
# If true, .env files and .htpasswd will NOT be copied into the repo.
EXCLUDE_DOTENV="${EXCLUDE_DOTENV:-true}"
EXCLUDE_HTPASSWD="${EXCLUDE_HTPASSWD:-true}"

# Optional human-readable label (e.g., "Clean Production Ready")
LABEL="${LABEL:-}"

# Commit/tag metadata
DATE_HUMAN="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
DATE_SAFE="$(date -u +'%Y%m%dT%H%M%SZ')"
HOSTTAG="$(hostname | tr -c '[:alnum:]' '-')"
LABEL_SAFE="$(printf '%s' "$LABEL" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '-' | sed 's/^-//;s/-$//')"
COMMIT_MSG="Hub backup ${DATE_HUMAN} on ${HOSTTAG}${LABEL:+ — $LABEL}"
TAG_BASE="hub-${HOSTTAG}-${DATE_SAFE}${LABEL_SAFE:+-$LABEL_SAFE}"

echo "[i] Hub backup started at $DATE_HUMAN (label: ${LABEL:-none})"

# ---------------------------
# Git identity & safety knobs
# ---------------------------
if ! git config --global user.email >/dev/null 2>&1; then
  git config --global user.email "pi@$(hostname -f || hostname)"
fi
if ! git config --global user.name >/dev/null 2>&1; then
  git config --global user.name  "Pi Hub ($(hostname))"
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
git fetch origin --tags --prune --prune-tags || true
git checkout main 2>/dev/null || git checkout -b main

# If tree is dirty before pulling, checkpoint or stash
if ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A || true
  git commit -m "checkpoint before sync: ${DATE_HUMAN}" || git stash --include-untracked
fi

git pull --rebase origin main || true

# ---------------------------
# Ensure repo layout
# ---------------------------
mkdir -p hub_server web_ui services/hub services/webui nginx backups/hub_db backups/logs/"$DATE_SAFE" backups/system/"$DATE_SAFE" secrets
LOGDIR="backups/logs/$DATE_SAFE"
SYSDIR="backups/system/$DATE_SAFE"

# ---------------------------
# Common rsync excludes (array → repeated --exclude)
# ---------------------------
EXCLUDES=(
  ".git/"
  "venv/"
  ".venv/"
  "__pycache__/"
  ".mypy_cache/"
  ".pytest_cache/"
  "*.pyc"
  "node_modules/"
  "dist/"
  "build/"
  ".next/"
)
if [ "${EXCLUDE_DOTENV}" = true ]; then
  EXCLUDES+=(".env" "*.env")
fi

rs_args() {
  local args=(-a --delete)
  for pat in "${EXCLUDES[@]}"; do args+=("--exclude=$pat"); done
  printf '%s\n' "${args[@]}"
}

# ---------------------------
# Sync hub_server runtime
# ---------------------------
if [ -d "$HUB_RUNTIME" ]; then
  echo "[i] Syncing hub_server runtime into repo..."
  rsync $(rs_args) "$HUB_RUNTIME/" "hub_server/"
else
  echo "[!] HUB_RUNTIME not found at $HUB_RUNTIME (skipping hub_server sync)"
fi

# ---------------------------
# Sync web_ui runtime
# ---------------------------
if [ -d "$WEBUI_RUNTIME" ]; then
  echo "[i] Syncing web_ui runtime into repo..."
  rsync $(rs_args) "$WEBUI_RUNTIME/" "web_ui/"
else
  echo "[!] WEBUI_RUNTIME not found at $WEBUI_RUNTIME (skipping web_ui sync)"
fi

# ---------------------------
# Discover additional services (auto)
# ---------------------------
echo "[i] Discovering hub/web/tft services..."
mapfile -t auto_hub < <(systemctl list-unit-files --type=service --no-legend | awk '{print $1}' | grep -E '^(hub-|tft-).*\.(service)$' | sed 's/\.service$//') || true
mapfile -t auto_web < <(systemctl list-unit-files --type=service --no-legend | awk '{print $1}' | grep -E '^(web-|hub-web-).*\.(service)$' | sed 's/\.service$//') || true
# Timers too
mapfile -t auto_timers < <(systemctl list-unit-files --type=timer --no-legend | awk '{print $1}' | sed 's/\.timer$/.timer/') || true

# Merge and unique
uniq_merge() {
  local -n base=$1
  shift
  for s in "$@"; do
    [[ " ${base[*]} " =~ " ${s} " ]] || base+=("$s")
  done
}

HUB_ALL=("${HUB_SERVICES[@]}")
WEB_ALL=("${WEBUI_SERVICES[@]}")

uniq_merge HUB_ALL "${auto_hub[@]:-}"
uniq_merge WEB_ALL "${auto_web[@]:-}"
uniq_merge WEB_ALL "${auto_timers[@]:-}"  # timers usually belong to web tasks here

# ---------------------------
# Save systemd service files + status + enablement + logs
# ---------------------------
save_service_unit() {
  local svc="$1"
  local dest_dir="$2"
  local unit_path="/etc/systemd/system/${svc}.service"

  if [ -f "$unit_path" ]; then
    sudo cp -f "$unit_path" "$dest_dir/${svc}.service" 2>/dev/null || true
    return 0
  fi

  if systemctl cat "$svc" >/dev/null 2>&1; then
    systemctl cat "$svc" | sudo tee "$dest_dir/${svc}.service" >/dev/null || true
    return 0
  fi
  return 1
}

capture_service_diag() {
  local svc="$1"
  local outbase="$2"
  {
    echo "### systemctl status $svc"
    systemctl status "$svc" --no-pager || true
    echo
    echo "### is-enabled $svc"
    systemctl is-enabled "$svc" || true
    echo
    echo "### show $svc (selected)"
    systemctl show "$svc" -p Id -p FragmentPath -p Description -p ActiveState -p SubState -p ExecStart || true
  } > "${outbase}-status.txt"
  sudo journalctl -u "$svc" -n 1000 --no-pager > "${outbase}.log" 2>/dev/null || true
}

echo "[i] Saving systemd service files (hub)..."
for svc in "${HUB_ALL[@]}"; do
  save_service_unit "$svc" "services/hub" || echo "[i] (hub) service not found: $svc"
  capture_service_diag "$svc" "$LOGDIR/${svc}"
done

echo "[i] Saving systemd service files (webui + timers)..."
for svc in "${WEB_ALL[@]}"; do
  save_service_unit "$svc" "services/webui" || echo "[i] (webui) service not found: $svc"
  capture_service_diag "$svc" "$LOGDIR/${svc}"
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
# Python env snapshots (hub & webui venvs if present)
# ---------------------------
if [ -x "$HUB_RUNTIME/venv/bin/pip" ]; then
  "$HUB_RUNTIME/venv/bin/pip" freeze > "backups/system/$DATE_SAFE/pip-freeze-hub.txt" || true
fi
if [ -x "$WEBUI_RUNTIME/venv/bin/pip" ]; then
  "$WEBUI_RUNTIME/venv/bin/pip" freeze > "backups/system/$DATE_SAFE/pip-freeze-webui.txt" || true
fi
python3 --version 2>/dev/null | tee "backups/system/$DATE_SAFE/python-version.txt" >/dev/null || true

# ---------------------------
# Nginx site + auth (optional)
# ---------------------------
if [ -f "$NGINX_SITE_AVAIL" ]; then
  echo "[i] Saving nginx site: $NGINX_SITE_AVAIL"
  sudo cp -f "$NGINX_SITE_AVAIL" "nginx/hub_web_admin.site" 2>/dev/null || true
  echo "$NGINX_SITE_LINK -> $NGINX_SITE_AVAIL" > "nginx/hub_web_admin.symlink.txt"
else
  echo "[i] Nginx site not found at $NGINX_SITE_AVAIL (skipping)"
fi

# Optional: .htpasswd (SECRET). Copy to /secrets (gitignored) unless EXCLUDE_HTPASSWD=false
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
# System snapshot (like camera script)
# ---------------------------
{
  echo "UTC: $DATE_HUMAN"
  echo "Host: $HOSTTAG"
  echo "Kernel: $(uname -a)"
  echo "OS: $(grep PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d '"'"'")"
  echo "GPU mem: $(vcgencmd get_mem gpu 2>/dev/null || echo n/a)"
  echo "Camera: $(vcgencmd get_camera 2>/dev/null || echo n/a)"
  echo
  echo "User/groups (pi): $(id -nG pi 2>/dev/null || true)"
  echo
  echo "Interfaces:"; ip -br a || true; echo
  echo "Routes:"; ip route || true; echo
  echo "WiFi networks (recent):"
  sudo awk -F= '/ssid=/{print $2}' /etc/NetworkManager/system-connections/* 2>/dev/null | sort -u || true
} > "$SYSDIR/host-summary.txt"

dpkg-query -W -f='${binary:Package}\t${Version}\n' > "$SYSDIR/packages.txt" || true
crontab -l > "$SYSDIR/crontab-pi.txt" 2>/dev/null || true
sudo test -f /etc/crontab && sudo cp -f /etc/crontab "$SYSDIR/crontab-root.txt" || true
sudo test -f /boot/config.txt && sudo cp -f /boot/config.txt "$SYSDIR/boot-config.txt" || true
sudo test -f /etc/modules && sudo cp -f /etc/modules "$SYSDIR/etc-modules.txt" || true
sudo test -d /etc/udev/rules.d && sudo rsync -a /etc/udev/rules.d/ "$SYSDIR/udev-rules.d/" || true

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
  echo "Hub services (explicit+auto):"
  for svc in "${HUB_ALL[@]}"; do
    printf "  %-28s %s\n" "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)"
  done
  echo
  echo "WebUI services (explicit+auto):"
  for svc in "${WEB_ALL[@]}"; do
    printf "  %-28s %s\n" "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)"
  done
  echo
  echo "Nginx site available: $NGINX_SITE_AVAIL"
  echo "Nginx site enabled:   $NGINX_SITE_LINK"
  echo "Basic auth file:      $NGINX_HTPASSWD (copied? $([ "${EXCLUDE_HTPASSWD}" = true ] && echo no || echo yes))"
  echo "WebUI env file:       $WEBUI_ENVFILE (copied? $([ "${EXCLUDE_DOTENV}" = true ] && echo no || echo yes))"
  echo
  echo "Label: ${LABEL:-none}"
} > "$LOGDIR/manifest.txt"

# ---------------------------
# Tarballs of mirrors (for quick restore)
# ---------------------------
if [ -d "hub_server" ]; then
  tar -C "hub_server" -czf "backups/logs/$DATE_SAFE/hub_server-$HOSTTAG-$DATE_SAFE${LABEL_SAFE:+-$LABEL_SAFE}.tar.gz" .
fi
if [ -d "web_ui" ]; then
  tar -C "web_ui" -czf "backups/logs/$DATE_SAFE/web_ui-$HOSTTAG-$DATE_SAFE${LABEL_SAFE:+-$LABEL_SAFE}.tar.gz" .
fi

# ---------------------------
# Commit & push & tag
# ---------------------------
echo "[i] Committing changes..."
git add hub_server web_ui services/hub services/webui nginx backups/hub_db "$LOGDIR" "$SYSDIR" secrets 2>/dev/null || true
git commit -m "$COMMIT_MSG" || echo "[i] Nothing to commit."

if ! git push origin main; then
  echo "[i] Push rejected; rebasing on origin/main and retrying..."
  git fetch origin
  git rebase origin/main || true
  git push origin main
fi

echo "[i] Tagging $TAG_BASE"
git tag -a "$TAG_BASE" -m "$COMMIT_MSG" || true
git push origin "$TAG_BASE" || true

echo "[✓] Hub backup complete → $REPO_DIR (tag: $TAG_BASE)"
