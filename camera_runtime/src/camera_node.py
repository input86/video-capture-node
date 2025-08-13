#!/usr/bin/env python3
import os
import time
import yaml
import queue
import threading
import requests
import shutil
import signal
import sys
from datetime import datetime
from pathlib import Path
from subprocess import run, CalledProcessError

import board, busio
from adafruit_vl53l0x import VL53L0X
from gpiozero import LED
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder

# ==========
# Config / Paths
# ==========
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
ROOT_DIR = Path.home() / "camera_node"
QUEUE_DIR = ROOT_DIR / "queue"     # holds failed uploads for retry

QUEUE_DIR.mkdir(parents=True, exist_ok=True)

# ==========
# LED Controller
# ==========
# Wiring: LED on GPIO27 (pin 13) with ~330Ω to GND (pin 14)
LED_PIN = 27

class LedController:
    """
    Modes:
      - 'idle': solid on
      - 'recording': fast blink (0.1s on, 0.1s off)
      - 'error': 3-burst pattern (three 0.25s blinks, then 1.5s pause), repeats while queue has items
    """
    def __init__(self, pin=LED_PIN):
        self.led = LED(pin)
        self._mode = "idle"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_mode(self, mode: str):
        with self._lock:
            self._mode = mode

    def stop(self):
        self._stop.set()
        self.led.off()

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                mode = self._mode

            if mode == "idle":
                # solid on
                self.led.on()
                time.sleep(0.2)

            elif mode == "recording":
                # fast blink
                self.led.on()
                time.sleep(0.1)
                self.led.off()
                time.sleep(0.1)

            elif mode == "error":
                # three short blinks, then pause
                for _ in range(3):
                    self.led.on()
                    time.sleep(0.25)
                    self.led.off()
                    time.sleep(0.25)
                time.sleep(1.5)

            else:
                # unknown mode -> off
                self.led.off()
                time.sleep(0.2)

# ==========
# Helpers
# ==========
def utc_ts():
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def free_space_ok(base="/"):
    st = os.statvfs(base)
    pct_free = (st.f_bavail / st.f_blocks) * 100.0 if st.f_blocks else 0.0
    return pct_free > MIN_FREE_PCT, pct_free

def log(msg):
    print(msg, flush=True)

# ==========
# Camera & Sensor Init
# ==========
log("[INIT] Initializing VL53L0X sensor and camera...")
i2c = busio.I2C(board.SCL, board.SDA)
sensor = VL53L0X(i2c)

picam2 = Picamera2()
video_config = picam2.create_video_configuration(
    main={"size": REC_RES}
)
picam2.configure(video_config)
picam2.start()

# ==========
# Upload Worker (background)
# ==========
upload_queue: "queue.Queue[Path]" = queue.Queue()
stop_event = threading.Event()

def do_upload(file_path: Path) -> bool:
    """Attempt to upload a file; return True on success, False otherwise."""
    url = f"{HUB_URL}/api/v1/clips"
    headers = {"X-Auth-Token": AUTH_TOKEN}
    try:
        with file_path.open("rb") as f:
            files = {"file": (file_path.name, f, "video/mp4")}
            r = requests.post(url, headers=headers, files=files, timeout=15)
        if r.status_code == 200:
            log(f"[UPLOAD] OK: {file_path.name}")
            return True
        else:
            log(f"[UPLOAD] Failed (status {r.status_code}) for {file_path.name}")
            return False
    except Exception as e:
        log(f"[UPLOAD] Exception for {file_path.name}: {e}")
        return False

def uploader_thread_fn(led: LedController):
    """Consumes the upload_queue and uploads files; on failure, move to QUEUE_DIR."""
    while not stop_event.is_set():
        try:
            file_path: Path = upload_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        ok = do_upload(file_path)
        if ok:
            try:
                file_path.unlink(missing_ok=True)
            except Exception as e:
                log(f"[UPLOAD] Cleanup error for {file_path.name}: {e}")
        else:
            # Move to queue dir for retry
            try:
                QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                target = QUEUE_DIR / file_path.name
                if file_path.resolve() != target.resolve():
                    shutil.move(str(file_path), str(target))
                log(f"[UPLOAD] Queued for retry: {target.name}")
            except Exception as e:
                log(f"[UPLOAD] Failed to move to retry queue: {e}")
        upload_queue.task_done()

def retry_scanner_thread_fn(led: LedController):
    """Periodically scans QUEUE_DIR for .mp4 files and re-enqueues them."""
    while not stop_event.is_set():
        try:
            queued = sorted([p for p in QUEUE_DIR.glob("*.mp4") if p.is_file()])
            if queued:
                # Indicate error mode while queue has items
                led.set_mode("error")
                for p in queued:
                    # re-enqueue if not already in-flight
                    upload_queue.put(p)
            else:
                # No pending retries -> idle (unless recording overrides)
                led.set_mode("idle")
        except Exception as e:
            log(f"[RETRY] Scanner exception: {e}")
        # Scan every 30 seconds
        for _ in range(30):
            if stop_event.is_set():
                break
            time.sleep(1.0)

# ==========
# Recording
# ==========
def record_clip() -> Path:
    """
    Record to /tmp/<node>_<ts>.mp4 using H264Encoder (no re-encode).
    Return the mp4 path.
    """
    ts = utc_ts()
    h264 = TMP_DIR / f"{NODE_ID}_{ts}.h264"
    mp4 = TMP_DIR / f"{NODE_ID}_{ts}.mp4"

    encoder = H264Encoder()
    picam2.start_recording(encoder, str(h264))
    time.sleep(REC_DUR)
    picam2.stop_recording()

    try:
        # Fast remux to MP4
        run(["ffmpeg", "-y", "-i", str(h264), "-c", "copy", str(mp4)], check=True)
    except CalledProcessError as e:
        log(f"[RECORD] ffmpeg error: {e}")
        # If remux failed, try cleanup and raise
        try:
            h264.unlink(missing_ok=True)
        finally:
            raise
    finally:
        # Remove raw h264 whenever possible
        try:
            h264.unlink(missing_ok=True)
        except Exception:
            pass

    return mp4

# ==========
# Main Loop
# ==========
def main():
    led = LedController(LED_PIN)

    # Start background workers
    up_thread = threading.Thread(target=uploader_thread_fn, args=(led,), daemon=True)
    up_thread.start()

    retry_thread = threading.Thread(target=retry_scanner_thread_fn, args=(led,), daemon=True)
    retry_thread.start()

    last_trigger_ms = 0

    log("[READY] Camera node started. Standing by...")
    led.set_mode("idle")

    try:
        while not stop_event.is_set():
            # Read distance
            try:
                dist = sensor.range
            except Exception as e:
                log(f"[SENSOR] Read exception: {e}")
                time.sleep(0.1)
                continue

            # Debug distance log (throttled)
            # print(f"Distance: {dist} mm")

            if dist < THRESH_MM:
                now_ms = int(time.time() * 1000)
                if now_ms - last_trigger_ms > DEBOUNCE_MS:
                    last_trigger_ms = now_ms

                    # Check free space on / (root). Adjust base path if you prefer a specific mount.
                    ok, pct = free_space_ok("/")
                    if not ok:
                        log(f"[SPACE] Low free space ({pct:.1f}%). Skipping record.")
                        time.sleep(0.2)
                        continue

                    log("[TRIGGER] Proximity detected; starting recording.")
                    led.set_mode("recording")
                    try:
                        mp4_path = record_clip()
                        log(f"[RECORD] Saved: {mp4_path.name}")
                    except Exception as e:
                        log(f"[RECORD] Exception: {e}")
                        # Return LED to appropriate mode based on queue status
                        led.set_mode("error" if any(QUEUE_DIR.glob('*.mp4')) else "idle")
                        continue

                    # Enqueue for upload (non-blocking)
                    try:
                        upload_queue.put(mp4_path)
                    except Exception as e:
                        log(f"[QUEUE] Failed to enqueue upload: {e}")
                        # As a fallback, move file to retry queue
                        try:
                            shutil.move(str(mp4_path), str(QUEUE_DIR / mp4_path.name))
                        except Exception as e2:
                            log(f"[QUEUE] Fallback move failed: {e2}")

                    # After recording, LED mode depends on pending queue
                    led.set_mode("error" if any(QUEUE_DIR.glob('*.mp4')) else "idle")

            time.sleep(0.05)

    finally:
        led.stop()
        try:
            picam2.stop()
        except Exception:
            pass
        log("[EXIT] Camera node stopped.")

# ==========
# Graceful Shutdown
# ==========
def _handle_sig(signum, frame):
    stop_event.set()

signal.signal(signal.SIGINT, _handle_sig)
signal.signal(signal.SIGTERM, _handle_sig)

if __name__ == "__main__":
    main()
