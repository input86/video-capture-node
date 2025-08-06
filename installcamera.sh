#!/usr/bin/env bash

set -euo pipefail

# installcamera.sh: setup and backup Camera Node (Pi Zero 2 W)
# Usage:
#   ./installcamera.sh [install_directory] [git_remote]
# Example:
#   ./installcamera.sh /home/pi/camera_node git@github.com:input86/video-capture-node.git

INSTALL_DIR=${1:-/home/pi/camera_node}
GIT_REMOTE=${2:-}

# 1. System update and install dependencies
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
  libcamera-apps ffmpeg i2c-tools python3-rpi.gpio python3-picamera2

# 2. Create project directory
sudo rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# 3. Set up Python virtual environment
python3 -m venv venv
source venv/bin/activate

# 4. Install Python packages
pip install --upgrade pip
pip install gpiozero requests pyyaml adafruit-circuitpython-vl53l0x smbus2 adafruit-blinka RPi.GPIO

# 5. Create src directory and configuration
mkdir -p src
cat > src/config.example.yaml << 'EOF'
hub_url: "http://<hub-ip>:5000"
node_id: "node01"
auth_token: "YOUR_SHARED_SECRET"
sensor:
  threshold_mm: 1000
  debounce_ms: 200
recording:
  resolution: "1280x720"
  framerate: 30
  duration_s: 5
storage:
  max_clips: 100
  min_free_percent: 10
EOF

cp src/config.example.yaml config.yaml

# 6. Write camera_node.py
cat > src/camera_node.py << 'EOF'
#!/usr/bin/env python3
import time, os, requests, yaml
from datetime import datetime
import board, busio
from adafruit_vl53l0x import VL53L0X
from gpiozero import LED
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from subprocess import run

# Load configuration
cfg = yaml.safe_load(open("config.yaml"))
led = LED(17)

# Initialize sensor (auto-ranging)
i2c    = busio.I2C(board.SCL, board.SDA)
sensor = VL53L0X(i2c)

# Initialize camera
picam2 = Picamera2()
video_config = picam2.create_video_configuration(
    main={"size": tuple(map(int, cfg["recording"]["resolution"].split("x")))}
)
picam2.configure(video_config)
picam2.start()

def free_space_ok():
    st = os.statvfs("/")
    return (st.f_bavail / st.f_blocks) * 100 > cfg["storage"]["min_free_percent"]

def record_clip(mp4_path):
    h264_path = mp4_path.replace(".mp4", ".h264")
    encoder = H264Encoder()
    picam2.start_recording(encoder, h264_path)
    time.sleep(cfg["recording"]["duration_s"])
    picam2.stop_recording()
    run(["ffmpeg", "-y", "-i", h264_path, "-c", "copy", mp4_path], check=True)
    os.remove(h264_path)

def upload_and_cleanup(path):
    try:
        files   = {"file": open(path, "rb")}
        headers = {"X-Auth-Token": cfg["auth_token"]}
        url     = f"{cfg['hub_url'].rstrip('/')}/api/v1/clips"
        r = requests.post(url, files=files, headers=headers, timeout=10)
        if r.status_code == 200:
            os.remove(path)
        else:
            print(f"Upload failed, status: {r.status_code}")
    except Exception as e:
        print(f"Upload exception: {e}")

try:
    while True:
        try:
            dist = sensor.range
            if dist < cfg["sensor"]["threshold_mm"]:
                t0 = time.time()
                while time.time() - t0 < cfg["sensor"]["debounce_ms"] / 1000:
                    if sensor.range >= cfg["sensor"]["threshold_mm"]:
                        break
                else:
                    if free_space_ok():
                        ts  = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
                        mp4 = f"/tmp/{cfg['node_id']}_{ts}.mp4"
                        record_clip(mp4)
                        upload_and_cleanup(mp4)
        except Exception as e:
            print(f"Loop exception: {e}")
        time.sleep(0.1)
finally:
    picam2.stop()
EOF

# 7. Create systemd service
sudo tee /etc/systemd/system/camera-node.service << 'EOF'
[Unit]
Description=Camera Node Service
After=network.target

[Service]
User=pi
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/src/camera_node.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable camera-node
sudo systemctl restart camera-node

# 8. Optional Git backup
if [ -n "$GIT_REMOTE" ]; then
  git init
  git add .
  git commit -m "Backup camera-node code"
  git remote add origin "$GIT_REMOTE"
  git branch -M main
  git push -u origin main
fi

echo "Camera Node installation and backup complete. Edit config.yaml with your hub_url and auth_token."
