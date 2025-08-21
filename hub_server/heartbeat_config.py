import os

DATA_DIR = os.environ.get("HUB_DATA_DIR", "/home/pi/data")
DB_PATH = os.path.join(DATA_DIR, "hub.db")

# Heartbeat thresholds (seconds) used by /api/v1/nodes
HEARTBEAT_ONLINE_SEC = 10
HEARTBEAT_STALE_SEC  = 30

# Node shared secrets (env overrides supported)
NODE_TOKENS = {
    "cam01": os.environ.get("CAM01_TOKEN", "q1w2e3r4t5y6u7i8o9p0"),
    "cam02": os.environ.get("CAM02_TOKEN", "cam2q1w2e3r4t5y6u7i8o9p0"),
}

HOST = os.environ.get("HEARTBEAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("HEARTBEAT_PORT", "5050"))
