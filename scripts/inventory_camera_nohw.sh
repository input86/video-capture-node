#!/usr/bin/env bash
set -euo pipefail

# Camera Node Inventory (no I²C/camera probing) - safe, time-bounded
STAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
OUTDIR="$HOME/cam-inventory-$STAMP"
mkdir -p "$OUTDIR"/{paths,services,units,logs,configs,env,sys}

# Helper: run a command with a short timeout; never fail the whole script
run() { timeout --signal=INT 6s bash -lc "$*" 2>&1 || true; }

echo "[i] Writing inventory to: $OUTDIR"

# -------- Paths & sizes (shallow, filtered) --------
{
  echo "===== home listing ====="
  run "ls -la '$HOME'"
  echo
  echo "===== likely folders ====="
  run "ls -ld '$HOME'/*/ 2>/dev/null | grep -Ei 'camera|cam|node|video|project'"
  echo
  echo "===== two levels deep (filtered) ====="
  # Avoid giant dirs; stay within HOME; skip known heavy dirs
  run "nice -n 10 find '$HOME' -xdev -maxdepth 2 -type d \
    \\( -iname '*camera*' -o -iname '*cam*' -o -iname '*node*' -o -iname '*video*' -o -iname '*project*' \\) \
    -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/venv/*' -not -path '*/.venv/*'"
  echo
  echo "===== sizes (best effort) ====="
  run "du -sh '$HOME/camera_node' '$HOME/projects/video-capture-node' 2>/dev/null"
} > "$OUTDIR/paths/paths.txt"

# -------- Services (system & user) --------
run "systemctl list-units --type=service" > "$OUTDIR/services/running-services.txt"
run "systemctl list-unit-files"           > "$OUTDIR/services/unit-files.txt"
run "systemctl --user list-units --type=service" > "$OUTDIR/services/user-running-services.txt"

# Candidate service units (camera/node/heartbeat)
awk '{print $1}' "$OUTDIR/services/unit-files.txt" 2>/dev/null \
  | grep -Ei 'camera|cam|node|heartbeat' \
  > "$OUTDIR/services/candidate-units.txt" || true

# Dump unit definitions + recent logs
if [ -s "$OUTDIR/services/candidate-units.txt" ]; then
  while read -r svc; do
    {
      echo "===== $svc ====="
      run "systemctl cat $svc"
    } >> "$OUTDIR/units/units-cat.txt"
    run "journalctl -u $svc -n 250 --no-pager" > "$OUTDIR/logs/${svc}.log"
  done < "$OUTDIR/services/candidate-units.txt"
fi

# -------- Configs (fast, filtered) --------
{
  echo "===== config.yaml under camera paths ====="
  run "nice -n 10 find '$HOME' -xdev -maxdepth 3 -type f -name 'config.yaml' \
    -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/venv/*' -not -path '*/.venv/*'"
  echo
  echo "===== grep for common keys (shallow) ====="
  for d in "$HOME/camera_node" "$HOME/projects/video-capture-node"; do
    [ -d "$d" ] || continue
    run "nice -n 10 grep -RInE 'hub_url:|node_id:|auth_token:' '$d' \
      --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=venv --exclude-dir=.venv \
      --exclude='*.mp4' --exclude='*.log'"
  done
} > "$OUTDIR/configs/configs.txt"

# -------- Python / envs --------
{
  echo "===== python ====="
  run "python3 --version"
  run "which python3"
  echo
  echo "===== venvs ====="
  run "nice -n 10 find '$HOME' -xdev -maxdepth 3 \\( -type d -name venv -o -name .venv \\) -prune -print"
  echo
  echo "===== .env files ====="
  run "nice -n 10 find '$HOME' -xdev -maxdepth 3 -type f \\( -name '.env' -o -name '*.env' \\) -not -path '*/.git/*'"
} > "$OUTDIR/env/python_env.txt"

# -------- System info (no i2c/camera) --------
{
  echo "===== uname ====="
  uname -a
  echo
  echo "===== df -h ====="
  run "df -h"
  echo
  echo "===== lsblk -f ====="
  run "lsblk -f"
  echo
  echo "===== ip -br addr ====="
  run "ip -br addr"
  echo
  echo "===== listening sockets (common ports) ====="
  run "ss -tulpn | grep -E ':(22|80|443|3000|5000|5173|8000|8080)\\b'"
  echo
  echo "===== processes (python/node/etc) ====="
  run "ps -ef | egrep -i 'python|node|gunicorn|uvicorn|flask|fastapi|nginx|pm2' | grep -v egrep"
} > "$OUTDIR/sys/system.txt"

echo "[✓] Inventory written to: $OUTDIR"
