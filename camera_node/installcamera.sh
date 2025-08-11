#!/usr/bin/env bash
set -euo pipefail

# ==========================
# Camera Node Installer (Pi OS Bookworm)
# ==========================
# - Prompts for Hub URL, Node ID, Auth Token
# - Optional static IP via dhcpcd (applies only after your confirmation)
# - Creates venv with system site-packages
# - Installs camera-node + heartbeat services
#
# Usage:
#   ./installcamera.sh [install_directory] [git_remote]
# Example:
#   ./installcamera.sh /home/pi/camera_node git@github.com:you/your-repo.git
#
# Safe to re-run: will update packages, (re)write services, and keep existing src files
# unless you explicitly confirm a wipe.

INSTALL_DIR=${1:-/home/pi/camera_node}
GIT_REMOTE=${2:-}

PI_USER=${SUDO_USER:-pi}
PI_HOME=$(eval echo "~$PI_USER")

bold() { printf "\e[1m%s\e[0m\n" "$*"; }
note() { printf "\n\e[1m[NOTE]\e[0m %s\n" "$*"; }
ok()   { printf "\e[32m[OK]\e[0m %s\n" "$*"; }
warn() { printf "\e[33m[WARN]\e[0m %s\n" "$*"; }
err()  { printf "\e[31m[ERR]\e[0m %s\n" "$*"; }

confirm() {
  local prompt="${1:-Are you sure?} [y/N]: "
  read -r -p "$prompt" REPLY || true
  case "$REPLY" in
    [yY][eE][sS]|[yY]) return 0 ;;
    *) return 1 ;;
  esac
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing command: $1"; exit 1; }
}

# --- 0) Sanity checks
require_cmd sudo
require_cmd bash

bold "== Camera Node Installer =="

# --- 1) System update & core packages
bold "1) Updating apt and installing OS packages..."
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  libcamera-apps python3-libcamera python3-picamera2 \
  ffmpeg i2c-tools python3-rpi.gpio \
  dhcpcd5 git curl

ok "Base packages installed."

# --- 2) Ensure groups / interfaces
bold "2) Ensuring 'pi' is in video/i2c/gpio groups and I2C enabled..."
sudo usermod -aG video,i2c,gpio "$PI_USER" || true

# Enable I2C if raspi-config exists
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_i2c 0 || true
fi
ok "Groups set; I2C enabled (may require reboot if newly enabled)."

# --- 3) Prepare install directory
bold "3) Preparing install directory: $INSTALL_DIR"
if [[ -d "$INSTALL_DIR" ]]; then
  note "Install directory already exists."
  if confirm "Do you want to wipe and recreate '$INSTALL_DIR'?"; then
    sudo rm -rf "$INSTALL_DIR"
    ok "Old directory removed."
  else
    warn "Keeping existing directory."
  fi
fi
mkdir -p "$INSTALL_DIR/src" "$INSTALL_DIR/queue"
sudo chown -R "$PI_USER":"$PI_USER" "$INSTALL_DIR"
cd "$INSTALL_DIR"

# --- 4) Optional git sync
if [[ -n "$GIT_REMOTE" ]]; then
  bold "4) Syncing from git remote: $GIT_REMOTE"
  if [[ -d .git ]]; then
    git fetch --all || true
    git reset --hard origin/main || true
  else
    git init
    git remote add origin "$GIT_REMOTE" || true
    git fetch origin
    git checkout -b main origin/main || git checkout -b main || true
  fi
  ok "Git sync complete."
else
  note "No git remote provided (optional). Proceeding with local files."
fi

# --- 5) Create / refresh venv (with system site packages so libcamera works)
bold "5) Creating Python venv (system-site-packages) and installing Python deps..."
if [[ -d venv ]]; then
  warn "venv already exists. Reusing."
else
  python3 -m venv --system-site-packages venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip
pip install --no-cache-dir \
  requests pyyaml gpiozero adafruit-circuitpython-vl53l0x adafruit-blinka

ok "Python deps installed in venv."

# --- 6) Gather node settings
bold "6) Node configuration"

default_hub="http://<hub-ip>:5000"
default_node="cam01"

read -r -p "Hub URL [$default_hub]: " HUB_URL || true
HUB_URL=${HUB_URL:-$default_hub}

read -r -p "Node ID [$default_node]: " NODE_ID || true
NODE_ID=${NODE_ID:-$default_node}

read -r -p "Auth Token (no default, required): " AUTH_TOKEN || true
if [[ -z "${AUTH_TOKEN:-}" ]]; then
  err "Auth Token is required."; exit 1
fi

# Sensor / recording defaults (you can tweak)
THRESH_MM=${THRESH_MM:-1000}
DEBOUNCE_MS=${DEBOUNCE_MS:-200}
RES=${RES:-"1280x720"}
FPS=${FPS:-30}
DUR_S=${DUR_S:-5}
MIN_FREE=${MIN_FREE:-10}
HB_INT=${HB_INT:-10}

# Write config.yaml (idempotent: overwrite with current answers)
cat > "$INSTALL_DIR/config.yaml" <<EOF
hub_url: "$HUB_URL"
node_id: "$NODE_ID"
auth_token: "$AUTH_TOKEN"

sensor:
  threshold_mm: $THRESH_MM
  debounce_ms: $DEBOUNCE_MS

recording:
  resolution: "$RES"
  framerate: $FPS
  duration_s: $DUR_S

storage:
  min_free_percent: $MIN_FREE

heartbeat_interval_sec: $HB_INT
EOF

ok "Wrote $INSTALL_DIR/config.yaml"

# Also drop an example for future reference (only if missing)
if [[ ! -f "$INSTALL_DIR/config.example.yaml" ]]; then
cat > "$INSTALL_DIR/config.example.yaml" <<'EOF'
hub_url: "http://<hub-ip>:5000"
node_id: "camXX"
auth_token: "YOUR_SHARED_SECRET"

sensor:
  threshold_mm: 1000
  debounce_ms: 200

recording:
  resolution: "1280x720"
  framerate: 30
  duration_s: 5

storage:
  min_free_percent: 10

heartbeat_interval_sec: 10
EOF
fi

# --- 7) Write heartbeat client if missing
if [[ ! -f "$INSTALL_DIR/heartbeat_client.py" ]]; then
  bold "7) Installing heartbeat_client.py"
  cat > "$INSTALL_DIR/heartbeat_client.py" <<'PY'
#!/usr/bin/env python3
import os, time, json, requests
from datetime import datetime, timezone

CONFIG_PATHS = [
    os.environ.get("CN_CONFIG") or "/home/pi/camera_node/config.yaml",
    "/home/pi/camera_node/config.json",
]

def load_config():
    for p in CONFIG_PATHS:
        if p and os.path.exists(p):
            with open(p, "r") as f:
                txt = f.read().strip()
                # Try JSON first
                try:
                    cfg = json.loads(txt)
                    print(f"[hb] loaded JSON config: {p}", flush=True)
                    return cfg
                except Exception:
                    # tiny yaml reader for our keys
                    d = {}
                    for line in txt.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"): continue
                        if ":" in line:
                            k, v = line.split(":", 1)
                            d[k.strip()] = v.strip().strip('"').strip("'")
                    print(f"[hb] loaded YAML config: {p}", flush=True)
                    hbsec = d.get("heartbeat_interval_sec", "10")
                    try: hbsec = int(hbsec)
                    except: hbsec = 10
                    return {
                        "hub_url": d.get("hub_url"),
                        "node_id": d.get("node_id"),
                        "auth_token": d.get("auth_token"),
                        "heartbeat_interval_sec": hbsec,
                    }
    raise FileNotFoundError("No config.yaml or config.json found")

def min_free_percent(path="/home/pi"):
    st = os.statvfs(path)
    free = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    return (free / total) * 100.0 if total else 0.0

def queue_length(qdir="/home/pi/camera_node/queue"):
    if not os.path.isdir(qdir): return 0
    return len([f for f in os.listdir(qdir) if f.endswith(".mp4")])

def main():
    cfg = load_config()
    hub_url   = (cfg.get("hub_url") or "").rstrip("/")
    node_id   = cfg.get("node_id") or "cam01"
    token     = cfg.get("auth_token") or "YOUR_SHARED_SECRET"
    interval  = int(cfg.get("heartbeat_interval_sec") or 10)

    hb_endpoint = f"{hub_url.replace(':5000', ':5050')}/api/v1/heartbeat" if ":5000" in hub_url else f"{hub_url}/api/v1/heartbeat"
    print(f"[hb] endpoint: {hb_endpoint}, node: {node_id}, interval: {interval}s", flush=True)

    backoff = 1
    while True:
        try:
            payload = {
                "node_id": node_id,
                "version": "camnode-1.0.0",
                "free_space_pct": round(min_free_percent("/home/pi"), 2),
                "queue_len": queue_length(),
            }
            print(f"[hb] POST {hb_endpoint} payload={payload}", flush=True)
            r = requests.post(
                hb_endpoint, json=payload,
                headers={"X-Auth-Token": token},
                timeout=4
            )
            print(f"[hb] status={r.status_code} body={r.text[:120]}", flush=True)
            if 200 <= r.status_code < 300:
                backoff = 1
                time.sleep(max(3, interval))
            else:
                backoff = min(30, backoff * 2)
                time.sleep(backoff)
        except Exception as e:
            print(f"[hb] error: {e}", flush=True)
            backoff = min(30, backoff * 2)
            time.sleep(backoff)

if __name__ == "__main__":
    main()
PY
  chmod +x "$INSTALL_DIR/heartbeat_client.py"
  ok "heartbeat_client.py installed."
else
  note "heartbeat_client.py already present; leaving as-is."
fi

# --- 8) Write camera_node.py only if missing (keeps your current version)
if [[ ! -f "$INSTALL_DIR/src/camera_node.py" ]]; then
  bold "8) Installing minimal camera_node.py (you can replace with your repo version)"
  cat > "$INSTALL_DIR/src/camera_node.py" <<'PY'
#!/usr/bin/env python3
import os, time, yaml, requests, shutil, queue, threading
from datetime import datetime
from pathlib import Path
from subprocess import run, CalledProcessError
import board, busio
from adafruit_vl53l0x import VL53L0X
from gpiozero import LED
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder

CFG = yaml.safe_load(open("config.yaml"))
NODE_ID = CFG["node_id"]
HUB_URL = CFG["hub_url"].rstrip("/")
AUTH_TOKEN = CFG["auth_token"]
REC_RES = tuple(map(int, CFG["recording"]["resolution"].split("x")))
REC_FPS = int(CFG["recording"]["framerate"])
REC_DUR = int(CFG["recording"]["duration_s"])
THRESH_MM = int(CFG["sensor"]["threshold_mm"])
DEBOUNCE_MS = int(CFG["sensor"]["debounce_ms"])
MIN_FREE_PCT = int(CFG["storage"]["min_free_percent"])

TMP_DIR = Path("/tmp")
QUEUE_DIR = Path.home() / "camera_node" / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

led = LED(27)

def utc_ts(): return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def free_space_ok(base="/"):
    st = os.statvfs(base)
    pct_free = (st.f_bavail / st.f_blocks) * 100.0 if st.f_blocks else 0.0
    return pct_free > MIN_FREE_PCT, pct_free

# Sensor
i2c    = busio.I2C(board.SCL, board.SDA)
sensor = VL53L0X(i2c)

# Camera
picam2 = Picamera2()
picam2.configure(picam2.create_video_configuration(main={"size": REC_RES}))
picam2.start()

def record_clip() -> Path:
    ts = utc_ts()
    h264 = TMP_DIR / f"{NODE_ID}_{ts}.h264"
    mp4  = TMP_DIR / f"{NODE_ID}_{ts}.mp4"
    enc = H264Encoder()
    picam2.start_recording(enc, str(h264))
    time.sleep(REC_DUR)
    picam2.stop_recording()
    run(["ffmpeg","-y","-i",str(h264),"-c","copy",str(mp4)], check=True)
    try: h264.unlink(missing_ok=True)
    except: pass
    return mp4

def upload(path: Path):
    try:
        with path.open("rb") as f:
            files = {"file": (path.name, f, "video/mp4")}
            r = requests.post(f"{HUB_URL}/api/v1/clips", files=files, headers={"X-Auth-Token": AUTH_TOKEN}, timeout=15)
        if r.status_code == 200:
            path.unlink(missing_ok=True)
        else:
            shutil.move(str(path), str(QUEUE_DIR / path.name))
    except Exception:
        shutil.move(str(path), str(QUEUE_DIR / path.name))

def main():
    last_ms = 0
    print("[READY] camera node running", flush=True)
    try:
        while True:
            try:
                dist = sensor.range
            except Exception as e:
                print("[SENSOR] read error:", e, flush=True)
                time.sleep(0.1); continue

            if dist < THRESH_MM:
                now = int(time.time() * 1000)
                if now - last_ms > DEBOUNCE_MS:
                    last_ms = now
                    ok, pct = free_space_ok("/")
                    if not ok:
                        print(f"[SPACE] low free space {pct:.1f}%", flush=True)
                        time.sleep(0.2); continue
                    print("[TRIGGER] start record", flush=True)
                    mp4 = record_clip()
                    print(f"[RECORD] saved {mp4.name}", flush=True)
                    upload(mp4)
            time.sleep(0.05)
    finally:
        try: picam2.stop()
        except: pass

if __name__ == "__main__":
    main()
PY
  chmod +x "$INSTALL_DIR/src/camera_node.py"
  ok "camera_node.py installed (minimal)."
else
  note "camera_node.py already present; leaving as-is."
fi

# --- 9) Systemd services
bold "9) Installing systemd services..."

# camera-node.service (uses INSTALL_DIR variables)
sudo tee /etc/systemd/system/camera-node.service >/dev/null <<EOF
[Unit]
Description=Camera Node Service
After=network.target

[Service]
User=$PI_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/src/camera_node.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# camera-heartbeat.service
sudo tee /etc/systemd/system/camera-heartbeat.service >/dev/null <<EOF
[Unit]
Description=Camera Heartbeat Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PI_USER
WorkingDirectory=$INSTALL_DIR
Environment=CN_CONFIG=$INSTALL_DIR/config.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/heartbeat_client.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable camera-node camera-heartbeat
sudo systemctl restart camera-node camera-heartbeat
ok "Services enabled & restarted."

# --- 10) Optional Static IP via dhcpcd (deferred apply)
bold "10) Optional static IP setup (dhcpcd)"
if confirm "Do you want to PREPARE a static IP now (apply after you confirm a restart)?"; then
  read -r -p "Interface (default eth0 or wlan0): " IFACE || true
  IFACE=${IFACE:-eth0}
  read -r -p "Static IP (e.g. 192.168.1.50/24): " STATIC_CIDR || true
  read -r -p "Router/Gateway IP (e.g. 192.168.1.1): " ROUTER_IP || true
  read -r -p "DNS (comma-separated, e.g. 1.1.1.1,8.8.8.8) [optional]: " DNS_LIST || true

  sudo install -m 644 -T /etc/dhcpcd.conf /etc/dhcpcd.conf.backup-$(date +%Y%m%d%H%M%S) || true
  # remove prior CAMNODE block if present
  sudo sed -i '/^# CAMNODE static IP START$/,/# CAMNODE static IP END$/{d}' /etc/dhcpcd.conf

  sudo tee -a /etc/dhcpcd.conf >/dev/null <<EOF

# CAMNODE static IP START
interface $IFACE
static ip_address=$STATIC_CIDR
static routers=$ROUTER_IP
$( [[ -n "$DNS_LIST" ]] && echo "static domain_name_servers=${DNS_LIST// /}" )
# CAMNODE static IP END
EOF

  # Clear old dhcpcd leases
  sudo rm -f /var/lib/dhcpcd5/*.lease || true
  ok "Static IP block written to /etc/dhcpcd.conf and old leases cleared."

  if confirm "Apply the static IP now by restarting dhcpcd (may drop current SSH)?"; then
    sudo systemctl restart dhcpcd
    ok "dhcpcd restarted. Your IP may have changed."
  else
    note "Static IP is staged. It will apply on next reboot or when you run: sudo systemctl restart dhcpcd"
  fi
else
  note "Skipping static IP setup."
fi

bold "All done ✅"
echo
echo "Quick checks:"
echo "  journalctl -u camera-node -n 50 --no-pager"
echo "  journalctl -u camera-heartbeat -n 50 --no-pager"
echo
echo "If you just enabled I2C or changed groups, a reboot may be required:"
echo "  sudo reboot"
`