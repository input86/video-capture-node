#!/usr/bin/env python3
import os
import time
import yaml
import queue
import threading
import requests
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run, CalledProcessError
from typing import Optional, Tuple

import board, busio
from adafruit_vl53l0x import VL53L0X
from gpiozero import LED
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import Output  # for custom MJPEG sink

# libcamera Transform for rotation (0/90/180/270)
try:
    from libcamera import Transform
except Exception:
    Transform = None  # Fallback: if unavailable, rotation control is skipped

# ==========
# Config / Paths
# ==========
CFG = yaml.safe_load(open("config.yaml"))
NODE_ID = CFG["node_id"]
HUB_URL = CFG["hub_url"].rstrip("/")
AUTH_TOKEN = CFG["auth_token"]

# ------- Profiles (fixed bundles) -------
PROFILES = {
    "balanced_1080p30": {
        "resolution": (1920, 1080),
        "fps": 30,
        "gop": 60,
        "h264_level": "4.1",
        "default_bitrate_kbps": 14000,
        "min_bitrate_kbps": 12000,
        "max_bitrate_kbps": 18000,
        "default_rotation": 0
    },
    "action_1080p60": {
        "resolution": (1920, 1080),
        "fps": 60,
        "gop": 120,
        "h264_level": "4.2",
        "default_bitrate_kbps": 24000,
        "min_bitrate_kbps": 22000,
        "max_bitrate_kbps": 28000,
        "default_rotation": 0
    },
    "storage_saver_720p30": {
        "resolution": (1280, 720),
        "fps": 30,
        "gop": 60,
        "h264_level": "4.0",
        "default_bitrate_kbps": 7000,
        "min_bitrate_kbps": 6000,
        "max_bitrate_kbps": 10000,
        "default_rotation": 0
    },
    "night_low_noise_1080p30": {
        "resolution": (1920, 1080),
        "fps": 30,
        "gop": 60,
        "h264_level": "4.1",
        "default_bitrate_kbps": 18000,
        "min_bitrate_kbps": 16000,
        "max_bitrate_kbps": 22000,
        "default_rotation": 0
    },
    "smooth_720p60": {
        "resolution": (1280, 720),
        "fps": 60,
        "gop": 120,
        "h264_level": "4.1",
        "default_bitrate_kbps": 12000,
        "min_bitrate_kbps": 10000,
        "max_bitrate_kbps": 16000,
        "default_rotation": 0
    },
}

def _load_effective_video_settings(cfg: dict) -> Tuple[Tuple[int,int], int, Optional[int], int, Optional[str]]:
    """
    Return (RES_TUPLE, FPS_INT, BITRATE_BPS_INT|None, ROT_DEG_INT, used_profile_name or None)
    """
    profile_name = cfg.get("profile")
    if profile_name in PROFILES:
        p = PROFILES[profile_name]
        res = tuple(p["resolution"])
        fps = int(p["fps"])

        br_kbps = cfg.get("bitrate_kbps", p["default_bitrate_kbps"])
        try:
            br_kbps = int(br_kbps)
        except Exception:
            br_kbps = p["default_bitrate_kbps"]
        br_kbps = max(p["min_bitrate_kbps"], min(p["max_bitrate_kbps"], br_kbps))
        bitrate_bps = br_kbps * 1000

        rot_allowed = {0, 90, 180, 270}
        rot = cfg.get("rotation", p["default_rotation"])
        try:
            rot = int(rot)
        except Exception:
            rot = p["default_rotation"]
        if rot not in rot_allowed:
            rot = p["default_rotation"]

        return res, fps, bitrate_bps, rot, profile_name

    # Backward-compat (no profile)
    rec = cfg.get("recording", {})
    res = tuple(map(int, str(rec.get("resolution", "1280x720")).split("x")))
    fps = int(rec.get("framerate", rec.get("fps", 30)))
    br_kbps = cfg.get("bitrate_kbps", rec.get("bitrate_kbps"))
    bitrate_bps = int(br_kbps) * 1000 if br_kbps else None

    rot = cfg.get("rotation", rec.get("rotation", 0))
    try:
        rot = int(rot)
    except Exception:
        rot = 0
    if rot not in {0, 90, 180, 270}:
        rot = 0

    return res, fps, bitrate_bps, rot, None

# Pull effective settings once at startup
EFF_RES, EFF_FPS, EFF_BITRATE_BPS, EFF_ROT_DEG, EFF_PROFILE = _load_effective_video_settings(CFG)

REC_RES = EFF_RES
REC_FPS = EFF_FPS
REC_DUR = int(CFG["recording"]["duration_s"])

# Sensor thresholds
THRESH_MM = int(CFG.get("sensor", {}).get("threshold_mm", 1000))
DEBOUNCE_MS = int(CFG.get("sensor", {}).get("debounce_ms", 200))

# XSHUT safeguard
LED_PIN = 27  # status LED (pin 13)

def _coerce_xshut(cfg: dict) -> Optional[int]:
    raw = cfg.get("sensor", {}).get("xshut_gpio", None)
    try:
        if raw is None or str(raw).strip() == "":
            return 4
        return int(raw)
    except Exception:
        return 4

XSHUT_GPIO = _coerce_xshut(CFG)
MIN_FREE_PCT = int(CFG.get("storage", {}).get("min_free_percent", 10))

TMP_DIR = Path("/tmp")
ROOT_DIR = Path.home() / "camera_node"
QUEUE_DIR = ROOT_DIR / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

# ==========
# LED Controller
# ==========
class LedController:
    """
    Modes:
      - 'idle': solid on
      - 'recording': fast blink (0.1s on, 0.1s off)
      - 'error': 3-burst pattern … repeats while queue has items
      - 'live': slow pulse (0.5s on, 0.5s off)
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
                self.led.on(); time.sleep(0.2)
            elif mode == "recording":
                self.led.on(); time.sleep(0.1)
                self.led.off(); time.sleep(0.1)
            elif mode == "error":
                for _ in range(3):
                    self.led.on(); time.sleep(0.25)
                    self.led.off(); time.sleep(0.25)
                time.sleep(1.5)
            elif mode == "live":
                self.led.on(); time.sleep(0.5)
                self.led.off(); time.sleep(0.5)
            else:
                self.led.off(); time.sleep(0.2)

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
log("[CONFIG] raw=None will be applied in RECORD and LIVE configs")  # marker to confirm correct file is running

# Optional XSHUT pulse
if XSHUT_GPIO is not None:
    try:
        if int(XSHUT_GPIO) == LED_PIN:
            log(f"[SENSOR] WARNING: xshut_gpio ({XSHUT_GPIO}) conflicts with LED pin ({LED_PIN}); skipping XSHUT pulse.")
        else:
            xshut = LED(int(XSHUT_GPIO))
            xshut.off(); time.sleep(0.05)
            xshut.on(); time.sleep(0.6)
            log(f"[SENSOR] Pulsed XSHUT on GPIO{XSHUT_GPIO} (LOW 50ms -> HIGH).")
    except Exception as e:
        log(f"[SENSOR] XSHUT pulse failed: {e}")

# I2C + sensor
i2c = busio.I2C(board.SCL, board.SDA)
sensor = VL53L0X(i2c)

# Picamera2 with retry
def _make_picam2_with_retry(max_tries=6, delay=0.5):
    last_err = None
    for attempt in range(1, max_tries + 1):
        try:
            return Picamera2()
        except IndexError as e:
            last_err = e
            log(f"[CAMERA] Picamera2 not ready (attempt {attempt}/{max_tries}); retrying in {delay:.1f}s...")
            time.sleep(delay)
        except Exception as e:
            last_err = e
            log(f"[CAMERA] Init error (attempt {attempt}/{max_tries}): {e}; retrying in {delay:.1f}s...")
            time.sleep(delay)
    raise last_err if last_err else RuntimeError("Picamera2 init failed")

picam2 = _make_picam2_with_retry()

# Base (RECORD) configuration
transform_kw = {}
if Transform and EFF_ROT_DEG in {0, 90, 180, 270}:
    try:
        transform_kw["transform"] = Transform(rotation=EFF_ROT_DEG)
    except Exception:
        pass

controls = {}
try:
    period_us = int(1_000_000 / REC_FPS)
    controls["FrameDurationLimits"] = (period_us, period_us)
except Exception:
    pass

# ---- MINIMAL CHANGE: disable RAW stream to avoid IMX708 PDAF path
record_config = picam2.create_video_configuration(main={"size": REC_RES}, raw=None, **transform_kw)
picam2.configure(record_config)
if controls:
    try:
        picam2.set_controls(controls)
    except Exception:
        pass
picam2.start()

# ==========
# Upload Worker (background)
# ==========
upload_queue: "queue.Queue[Path]" = queue.Queue()
stop_event = threading.Event()

def do_upload(file_path: Path) -> bool:
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
            # --- NEW: if we just cleared the last queued file, flip LED back to idle (when not LIVE)
            try:
                if file_path.parent == QUEUE_DIR and MODE == "RECORD":
                    if not any(QUEUE_DIR.glob("*.mp4")):
                        led.set_mode("idle")
            except Exception as e:
                log(f"[LED] post-upload check exception: {e}")
        else:
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
    while not stop_event.is_set():
        try:
            queued = sorted([p for p in QUEUE_DIR.glob("*.mp4") if p.is_file()])
            if queued:
                # keep existing behavior: show 'error' while queued files exist (only if not LIVE)
                if MODE == "RECORD":
                    led.set_mode("error")
                for p in queued:
                    upload_queue.put(p)
            else:
                # --- NEW: nothing queued anymore; if not LIVE, ensure LED returns to idle
                if MODE == "RECORD":
                    led.set_mode("idle")
        except Exception as e:
            log(f"[RETRY] Scanner exception: {e}")
        for _ in range(30):
            if stop_event.is_set():
                break
            time.sleep(1.0)

# ==========
# Recording
# ==========
def record_clip() -> Path:
    ts = utc_ts()
    h264 = TMP_DIR / f"{NODE_ID}_{ts}.h264"
    mp4 = TMP_DIR / f"{NODE_ID}_{ts}.mp4"

    encoder = H264Encoder(bitrate=EFF_BITRATE_BPS) if EFF_BITRATE_BPS else H264Encoder()

    picam2.start_recording(encoder, str(h264))
    time.sleep(REC_DUR)
    picam2.stop_recording()

    try:
        run(["ffmpeg", "-y", "-i", str(h264), "-c", "copy", str(mp4)], check=True)
    except CalledProcessError as e:
        log(f"[RECORD] ffmpeg error: {e}")
        try:
            h264.unlink(missing_ok=True)
        finally:
            raise
    finally:
        try:
            h264.unlink(missing_ok=True)
        except Exception:
            pass

    return mp4

# ==========
# LIVE preview — HTTP server + MJPEG stream
# ==========
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import json

LIVE_LOCK = threading.RLock()
MODE = "RECORD"  # "RECORD" | "LIVE"
LIVE_ENCODER = None
LIVE_ACTIVE_CLIENTS = 0
LIVE_LAST_ACTIVITY = 0.0  # last time (monotonic) we sent a frame
LIVE_TIMEOUT_SEC = 120
MJPEG_QUALITY = 75
MJPEG_FPS = min(20, REC_FPS)
LIVE_LAST_ERROR = ""

# Single-client sink queue
class _FrameBus:
    def __init__(self):
        self._q = None
        self._lock = threading.Lock()

    def attach(self):
        with self._lock:
            if self._q is not None:
                try:
                    while True:
                        self._q.get_nowait()
                except Exception:
                    pass
            self._q = queue.Queue(maxsize=1)
            return self._q

    def detach(self):
        with self._lock:
            self._q = None

    def write(self, b: bytes):
        with self._lock:
            if self._q is None:
                return
            try:
                if self._q.full():
                    try:
                        self._q.get_nowait()
                    except Exception:
                        pass
                self._q.put_nowait(b)
            except Exception:
                pass

FRAMEBUS = _FrameBus()

class _MJPEGOutput(Output):
    """Receives JPEG frames from MJPEGEncoder and pushes into FRAMEBUS."""
    # Picamera2 will call with (frame, keyframe, timestamp, packet, audio)
    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=None):
        global LIVE_LAST_ACTIVITY
        try:
            # frame is already a full JPEG image (bytes-like)
            FRAMEBUS.write(frame)
            LIVE_LAST_ACTIVITY = time.monotonic()
        except Exception:
            pass

def _enter_live(led: LedController):
    global MODE, LIVE_ENCODER, LIVE_LAST_ACTIVITY, LIVE_LAST_ERROR
    with LIVE_LOCK:
        if MODE == "LIVE":
            return True
        try:
            picam2.stop()
        except Exception:
            pass

        transform_kw_live = {}
        if Transform and EFF_ROT_DEG in {0, 90, 180, 270}:
            try:
                transform_kw_live["transform"] = Transform(rotation=EFF_ROT_DEG)
            except Exception:
                pass

        # ---- MINIMAL CHANGE: disable RAW in LIVE config too
        live_config = picam2.create_video_configuration(
            main={"size": REC_RES, "format": "YUV420"},
            raw=None,
            **transform_kw_live
        )
        picam2.configure(live_config)

        try:
            period_us = int(1_000_000 / MJPEG_FPS)
            picam2.set_controls({"FrameDurationLimits": (period_us, period_us)})
        except Exception:
            pass

        try:
            picam2.start()
            try:
                LIVE_ENCODER = MJPEGEncoder(quality=MJPEG_QUALITY)
            except TypeError:
                LIVE_ENCODER = MJPEGEncoder()
            picam2.start_recording(LIVE_ENCODER, _MJPEGOutput())
        except Exception as e:
            LIVE_LAST_ERROR = f"start_recording failed: {e}"
            log(f"[LIVE] enter failed: {LIVE_LAST_ERROR}")
            try:
                picam2.stop()
                picam2.configure(record_config)
                if controls:
                    picam2.set_controls(controls)
                picam2.start()
            except Exception as e2:
                log(f"[LIVE] recovery failed: {e2}")
            return False

        MODE = "LIVE"
        LIVE_LAST_ERROR = ""
        LIVE_LAST_ACTIVITY = time.monotonic()
        led.set_mode("live")
        log("[LIVE] start")
        return True

def _exit_live(led: LedController):
    global MODE, LIVE_ENCODER
    with LIVE_LOCK:
        if MODE == "RECORD":
            return True
        try:
            try:
                picam2.stop_recording()
            except Exception:
                pass
            LIVE_ENCODER = None
            picam2.stop()
            picam2.configure(record_config)
            if controls:
                try:
                    picam2.set_controls(controls)
                except Exception:
                    pass
            picam2.start()
            MODE = "RECORD"
            led.set_mode("error" if any(QUEUE_DIR.glob('*.mp4')) else "idle")
            log("[LIVE] stop")
            return True
        except Exception as e:
            log(f"[LIVE→RECORD FAIL] {e}")
            led.set_mode("error")
            time.sleep(1.5)
            try:
                picam2.stop()
                picam2.configure(record_config)
                if controls:
                    try:
                        picam2.set_controls(controls)
                    except Exception:
                        pass
                picam2.start()
                MODE = "RECORD"
                led.set_mode("error" if any(QUEUE_DIR.glob('*.mp4')) else "idle")
                log("[LIVE] recovered to RECORD")
                return True
            except Exception as e2:
                log(f"[LIVE] unrecoverable: {e2}")
                return False

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def _json_response(handler: BaseHTTPRequestHandler, status: int, obj: dict):
    data = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)

from http import HTTPStatus

class LiveHandler(BaseHTTPRequestHandler):
    server_version = "CameraNodeLive/1.0"

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/live/start":
            ok = _enter_live(LED_GLOBAL)
            if ok:
                return _json_response(self, 200, {"ok": True, "state": "LIVE"})
            else:
                return _json_response(self, 500, {"ok": False, "error": LIVE_LAST_ERROR or "failed to enter LIVE"})
        if self.path == "/api/live/stop":
            ok = _exit_live(LED_GLOBAL)
            return _json_response(self, 200, {"ok": True, "state": "RECORD" if ok else "LIVE"})
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_GET(self):
        if self.path.startswith("/api/live/sensor"):
            try:
                dist = int(sensor.range)
            except Exception as e:
                return _json_response(self, 503, {"ok": False, "error": str(e)})
            would = bool(dist < THRESH_MM)
            obj = {
                "distance_mm": dist,
                "threshold_mm": THRESH_MM,
                "would_trigger": would,
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
            }
            return _json_response(self, 200, obj)

        if self.path.startswith("/api/live/mjpeg"):
            ok = _enter_live(LED_GLOBAL)
            if not ok:
                return _json_response(self, 500, {"ok": False, "error": LIVE_LAST_ERROR or "failed to enter LIVE"})

            boundary = "FRAME"
            self.send_response(HTTPStatus.OK)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary=--{boundary}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = FRAMEBUS.attach()
            try:
                while True:
                    now = time.monotonic()
                    if now - LIVE_LAST_ACTIVITY > LIVE_TIMEOUT_SEC:
                        log("[LIVE] timeout → reverting to RECORD")
                        _exit_live(LED_GLOBAL)
                        break

                    frame = q.get(timeout=1.0)
                    self.wfile.write(b"--" + boundary.encode() + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except Exception:
                pass
            finally:
                FRAMEBUS.detach()
            return

        _json_response(self, 404, {"ok": False, "error": "not found"})

def http_server_thread():
    srv = ThreadingHTTPServer(("0.0.0.0", 8080), LiveHandler)
    log("[READY] LIVE API listening on :8080")
    srv.serve_forever()

# ==========
# Main Loop
# ==========
def main():
    global LED_GLOBAL
    led = LedController(LED_PIN)
    LED_GLOBAL = led

    # Background workers
    threading.Thread(target=uploader_thread_fn, args=(led,), daemon=True).start()
    threading.Thread(target=retry_scanner_thread_fn, args=(led,), daemon=True).start()

    # HTTP server
    threading.Thread(target=http_server_thread, daemon=True).start()

    last_trigger_ms = 0

    prof_str = EFF_PROFILE if EFF_PROFILE else "legacy"
    br_str = f"{EFF_BITRATE_BPS//1000} kbps" if EFF_BITRATE_BPS else "default"
    log(f"[READY] Profile={prof_str} | Res={REC_RES[0]}x{REC_RES[1]} @ {REC_FPS}fps | Bitrate={br_str} | Rotation={EFF_ROT_DEG}°")
    led.set_mode("idle")

    try:
        while not stop_event.is_set():
            if MODE == "LIVE":
                time.sleep(0.1)
                continue

            try:
                dist = sensor.range
            except Exception as e:
                log(f"[SENSOR] Read exception: {e}")
                time.sleep(0.1)
                continue

            if dist < THRESH_MM:
                now_ms = int(time.time() * 1000)
                if now_ms - last_trigger_ms > DEBOUNCE_MS:
                    last_trigger_ms = now_ms

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
                        led.set_mode("error" if any(QUEUE_DIR.glob('*.mp4')) else "idle")
                        continue

                    try:
                        upload_queue.put(mp4_path)
                    except Exception as e:
                        log(f"[QUEUE] Failed to enqueue upload: {e}")
                        try:
                            shutil.move(str(mp4_path), str(QUEUE_DIR / mp4_path.name))
                        except Exception as e2:
                            log(f"[QUEUE] Fallback move failed: {e2}")

                    led.set_mode("error" if any(QUEUE_DIR.glob('*.mp4')) else "idle")

            time.sleep(0.05)

    finally:
        led.stop()
        try:
            if MODE == "LIVE":
                try:
                    picam2.stop_recording()
                except Exception:
                    pass
            picam2.stop()
        except Exception:
            pass
        log("[EXIT] Camera node stopped.")

# ==========
# Graceful Shutdown
# ==========
stop_event = threading.Event()
LED_GLOBAL: 'LedController'  # set in main()

def _handle_sig(signum, frame):
    stop_event.set()

signal.signal(signal.SIGINT, _handle_sig)
signal.signal(signal.SIGTERM, _handle_sig)

if __name__ == "__main__":
    main()
