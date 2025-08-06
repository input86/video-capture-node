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
