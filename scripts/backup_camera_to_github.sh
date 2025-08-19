#!/usr/bin/env bash
# === SAVE AS: ~/projects/video-capture-node/scripts/backup_camera_to_github.sh ===
set -euo pipefail

# =========
# Settings
# =========
REPO_SSH="${REPO_SSH:-git@github.com:input86/video-capture-node.git}"
REPO_DIR="${REPO_DIR:-$HOME/projects/video-capture-node}"

CAM_RUNTIME="${CAM_RUNTIME:-/home/pi/camera_node}"   # live code
CAM_REPO_DST="${CAM_REPO_DST:-camera_runtime}"       # repo mirror of live runtime

# Primary services you expect (explicit)
CAM_SERVICES=( ${CAM_SERVICES_OVERRIDE:-camera-node camera-heartbeat} )

# Optional human-readable label for this snapshot (e.g., "Clean Production Ready")
LABEL="${LABEL:-}"

DATE_HUMAN="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
DATE_SAFE="$(date -u +'%Y%m%dT%H%M%SZ')"
HOSTTAG="$(hostname | tr -c '[:alnum:]' '-')"
LABEL_SAFE="$(printf '%s' "$LABEL" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '-' | sed 's/^-//;s/-$//')"

# Extract node_id from config.yaml if present
NODE_ID="unknown"
if [ -f "$CAM_RUNTIME/config.yaml" ]; then
  NODE_ID="$(awk -F: '/^[[:space:]]*node_id[[:space:]]*:/ {gsub(/ /,"",$2); print $2; exit}' "$CAM_RUNTIME/config.yaml" 2>/dev/null || echo unknown)"
  [ -z "$NODE_ID" ] && NODE_ID="unknown"
fi

COMMIT_MSG="Camera backup ${DATE_HUMAN} on ${HOSTTAG}${LABEL:+ — $LABEL} (node_id=${NODE_ID})"
TAG_BASE="cam-${NODE_ID}-${HOSTTAG}-${DATE_SAFE}${LABEL_SAFE:+-$LABEL_SAFE}"

echo "[i] Camera backup started at $DATE_HUMAN (label: ${LABEL:-none}, node_id: ${NODE_ID})"

# =========
# Repo prep
# =========
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[i] Cloning repo into $REPO_DIR"
  git clone "$REPO_SSH" "$REPO_DIR"
fi
cd "$REPO_DIR"

# Git identity & safe defaults
git config --global user.email >/dev/null 2>&1 || git config --global user.email "pi@$(hostname -f || hostname)"
git config --global user.name  >/dev/null 2>&1 || git config --global user.name  "Pi Camera ($(hostname))"
git config --global pull.rebase true
git config --global rebase.autostash true
git config --global fetch.prune true
git config --global fetch.pruneTags true

git fetch origin --tags --prune --prune-tags || true
git checkout main 2>/dev/null || git checkout -b main

# If tree is dirty before pulling, checkpoint or stash
if ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A || true
  git commit -m "checkpoint before sync: ${DATE_HUMAN}" || git stash --include-untracked
fi

git pull --rebase origin main || true

# =========
# Layout
# =========
mkdir -p \
  "$CAM_REPO_DST" \
  services/camera \
  backups/logs/"$DATE_SAFE" \
  backups/camera \
  backups/system/"$DATE_SAFE"

LOGDIR="backups/logs/$DATE_SAFE"
SYSDIR="backups/system/$DATE_SAFE"
CAMBACK="backups/camera"

# =========
# Runtime → repo mirror (fixed excludes)
# =========
if [ -d "$CAM_RUNTIME" ]; then
  echo "[i] Syncing $CAM_RUNTIME -> $REPO_DIR/$CAM_REPO_DST ..."
  EXCLUDES=(
    ".git/"
    "venv/"
    ".venv/"
    "__pycache__/"
    ".mypy_cache/"
    ".pytest_cache/"
    "*.pyc"
    ".env"
    "*.env"
  )
  RSYNC_ARGS=(-a --delete)
  for pat in "${EXCLUDES[@]}"; do
    RSYNC_ARGS+=("--exclude=$pat")
  done
  rsync "${RSYNC_ARGS[@]}" "$CAM_RUNTIME/" "$CAM_REPO_DST/"
else
  echo "[!] Camera runtime not found at $CAM_RUNTIME (skipping code sync)"
fi

# =========
# Discover services
# =========
echo "[i] Discovering camera-* services..."
mapfile -t auto_svcs < <(systemctl list-unit-files --type=service --no-legend \
  | awk '{print $1}' | grep -E '^camera-.*\.service$' | sed 's/\.service$//') || true

# Merge unique service names
ALL_SERVICES=("${CAM_SERVICES[@]}")
for s in "${auto_svcs[@]:-}"; do
  if [[ ! " ${ALL_SERVICES[*]} " =~ " ${s} " ]]; then
    ALL_SERVICES+=("$s")
  fi
done

# =========
# Save service units + status + enablement + logs
# =========
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

echo "[i] Saving service files & diagnostics..."
for s in "${ALL_SERVICES[@]}"; do
  save_unit "$s"
  {
    echo "### systemctl status $s"
    systemctl status "$s" --no-pager || true
    echo
    echo "### is-enabled $s"
    systemctl is-enabled "$s" || true
    echo
    echo "### show $s (selected)"
    systemctl show "$s" -p Id -p FragmentPath -p Description -p ActiveState -p SubState -p ExecStart || true
  } > "$LOGDIR/${s}-status.txt"
  sudo journalctl -u "$s" -n 1000 --no-pager > "$LOGDIR/${s}.log" 2>/dev/null || true
done

# =========
# Python env snapshot
# =========
if [ -x "$CAM_RUNTIME/venv/bin/pip" ]; then
  "$CAM_RUNTIME/venv/bin/pip" freeze > "$CAMBACK/pip-freeze-$DATE_SAFE.txt" || true
fi
python3 --version 2>/dev/null | tee "$CAMBACK/python-version-$DATE_SAFE.txt" >/dev/null || true

# =========
# Config: raw + redacted
# =========
if [ -f "$CAM_RUNTIME/config.yaml" ]; then
  cp -f "$CAM_RUNTIME/config.yaml" "$CAMBACK/config-$DATE_SAFE.yaml"

  # Redact secrets (auth_tokens/auth-token/auth_token)
  awk '
    BEGIN{inblock=0}
    /^[[:space:]]*auth_tokens:/ {print "auth_tokens: {REDACTED: true}"; inblock=1; next}
    inblock && /^[^[:space:]]/ {inblock=0}
    {print}
  ' "$CAM_RUNTIME/config.yaml" \
  | sed -E 's/(auth[-_]?token[s]?[[:space:]]*:[[:space:]]*).*/\1REDACTED/i' \
  > "$CAMBACK/config-$DATE_SAFE.redacted.yaml"
fi

# =========
# System snapshot
# =========
{
  echo "UTC: $DATE_HUMAN"
  echo "Host: $HOSTTAG"
  echo "node_id: ${NODE_ID}"
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

# =========
# Minimal manifest (human friendly)
# =========
{
  echo "UTC: $DATE_HUMAN"
  echo "Host: $HOSTTAG"
  echo "node_id: ${NODE_ID}"
  echo "Runtime: $CAM_RUNTIME"
  echo "Repo dst: $CAM_REPO_DST"
  echo "Services (explicit): ${CAM_SERVICES[*]}"
  echo "Services (auto): ${auto_svcs[*]:-none}"
  echo "Label: ${LABEL:-none}"
} > "$LOGDIR/manifest.txt"

# =========
# Tarball of runtime (optional but handy)
# =========
if [ -d "$CAM_REPO_DST" ]; then
  TAR="$CAMBACK/camera-runtime-$NODE_ID-$HOSTTAG-$DATE_SAFE${LABEL_SAFE:+-$LABEL_SAFE}.tar.gz"
  tar -C "$CAM_REPO_DST" -czf "$TAR" .
fi

# =========
# Commit + push
# =========
git add "$CAM_REPO_DST" services/camera "$LOGDIR" "$CAMBACK" "$SYSDIR" || true
git commit -m "$COMMIT_MSG" || echo "[i] Nothing to commit."

if ! git push origin main; then
  echo "[i] Push rejected; rebasing on origin/main and retrying..."
  git fetch origin
  git rebase origin/main || true
  git push origin main
fi

# Tag with timestamp + host + node_id (+label)
echo "[i] Tagging $TAG_BASE"
git tag -a "$TAG_BASE" -m "$COMMIT_MSG" || true
git push origin "$TAG_BASE" || true

echo "[✓] Camera backup complete → $REPO_DIR (tag: $TAG_BASE)"
