#!/usr/bin/env bash
set -euo pipefail

# register_camera.sh
# Usage:
#   ./register_camera.sh cam02 192.168.0.152 z9y8x7w6v5u4t3s2r1 [pi] [/home/pi/camera_node/config.yaml] [camera-node]
#
# - Adds cam + token to hub config.yaml
# - Adds SSH known_host for IP
# - Copies hub's SSH key to node for passwordless access
# - Saves node endpoint into WebUI via local API
# - Restarts hub web admin

CAM_ID="${1:-}"
CAM_IP="${2:-}"
CAM_TOKEN="${3:-}"
SSH_USER="${4:-pi}"
CONFIG_PATH="${5:-/home/pi/camera_node/config.yaml}"
SERVICE_NAME="${6:-camera-node}"

if [[ -z "$CAM_ID" || -z "$CAM_IP" || -z "$CAM_TOKEN" ]]; then
  echo "Usage: $0 <cam_id> <cam_ip> <token> [ssh_user] [config_path] [service_name]"
  exit 1
fi

HUB_CFG="/home/pi/hub_server/config.yaml"
WEB_API="http://127.0.0.1"
SYSTEMD_UNIT="hub-web-admin"

echo "[1/7] Ensuring SSH key exists on hub..."
if [[ ! -f "$HOME/.ssh/id_ed25519.pub" ]]; then
  ssh-keygen -t ed25519 -C hub-server -N '' -f "$HOME/.ssh/id_ed25519"
fi

echo "[2/7] Updating hub config.yaml auth_tokens for ${CAM_ID}..."
python3 - "$HUB_CFG" "$CAM_ID" "$CAM_TOKEN" <<'PY'
import sys, yaml, os
cfg_path, cam_id, token = sys.argv[1], sys.argv[2], sys.argv[3]
with open(cfg_path, 'r') as f:
    cfg = yaml.safe_load(f) or {}
cfg.setdefault('auth_tokens', {})
cfg['auth_tokens'][cam_id] = token
tmp = cfg_path + ".tmp"
with open(tmp, 'w') as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
os.replace(tmp, cfg_path)
print("OK: token updated")
PY

echo "[3/7] Trusting SSH host key for ${CAM_IP}..."
# remove stale keys for this IP (safe even if none)
ssh-keygen -R "${CAM_IP}" >/dev/null 2>&1 || true
# add new keys
mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
ssh-keyscan -H -t ed25519 "${CAM_IP}" >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
ssh-keyscan -H -t rsa     "${CAM_IP}" >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
chmod 600 "$HOME/.ssh/known_hosts"

echo "[4/7] Copying hub public key to ${SSH_USER}@${CAM_IP}..."
# This will prompt for the node password once (after reimage)
ssh-copy-id -o StrictHostKeyChecking=yes "${SSH_USER}@${CAM_IP}"

echo "[5/7] Saving endpoint into WebUI..."
# These endpoints come from our web admin app (available locally on the hub).
# If your app is protected, make sure this call is allowed from localhost.
curl -sS -X POST "${WEB_API}/action/secure/cameras/save_endpoint" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg cam "$CAM_ID" --arg host "$CAM_IP" --arg user "$SSH_USER" \
             --arg path "$CONFIG_PATH" --arg svc "$SERVICE_NAME" \
             '{camera_id:$cam, ssh_host:$host, ssh_user:$user, config_path:$path, service_name:$svc}')" >/dev/null

echo "[6/7] Restarting WebUI..."
sudo systemctl restart "${SYSTEMD_UNIT}"

echo "[7/7] Verifying connection to node..."
if ssh -o BatchMode=yes "${SSH_USER}@${CAM_IP}" -- 'echo OK && hostnamectl --static'; then
  echo "✅ Registration complete for ${CAM_ID} (${CAM_IP})."
  echo "   You can now open the WebUI → Camera Settings → \"${CAM_ID}\" to push config or preview."
else
  echo "⚠️  Registered, but SSH check failed (no passwordless auth?). Try running ssh-copy-id again."
fi
