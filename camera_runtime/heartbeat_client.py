# === SAVE AS: /home/pi/camera_node/heartbeat_client.py ===
#!/usr/bin/env python3
import os
import time
import json
import shutil
import socket
from pathlib import Path
from typing import Tuple

import yaml
import requests

CONFIG_PATH = os.environ.get("CN_CONFIG", "/home/pi/camera_node/config.yaml")
INSTALL_DIR = Path(__file__).resolve().parent
QUEUE_DIR = Path(os.environ.get("CN_QUEUE", str(INSTALL_DIR / "queue")))

def load_cfg() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}

def ensure_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        raise ValueError("hub_url missing in config.yaml")
    # Add scheme if missing
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "http://" + u
    return u.rstrip("/")

def get_storage_base(cfg: dict) -> Path:
    base = cfg.get("storage", {}).get("base_dir")
    return Path(base) if base else INSTALL_DIR

def free_space_pct(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path)
        return round((usage.free / usage.total) * 100, 2)
    except Exception:
        return -1.0

def queue_len() -> int:
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        return sum(1 for p in QUEUE_DIR.iterdir() if p.is_file())
    except Exception:
        return 0

def post_heartbeat(ep: str, token: str, payload: dict, timeout: float = 6.0) -> Tuple[int, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Auth-Token": token,
    }
    try:
        r = requests.post(ep, headers=headers, data=json.dumps(payload), timeout=timeout)
        if r.status_code == 401:
            # Some older hubs used Authorization: Bearer
            headers.pop("X-Auth-Token", None)
            headers["Authorization"] = f"Bearer {token}"
            r = requests.post(ep, headers=headers, data=json.dumps(payload), timeout=timeout)
        return r.status_code, r.text.strip()
    except requests.RequestException as e:
        return -1, f"{type(e).__name__}: {e}"

def main():
    cfg = load_cfg()
    hub_url = ensure_url(cfg.get("hub_url", ""))
    endpoint = f"{hub_url}/api/v1/heartbeat"

    node_id = cfg.get("node_id", "cam01")
    auth_token = cfg.get("auth_token", "")
    interval = int(cfg.get("heartbeat_interval_sec", 10))

    if not auth_token:
        print("[hb] error: auth_token missing in config.yaml")
        return

    storage_base = get_storage_base(cfg)
    host = socket.gethostname()
    print(f"[hb] loaded YAML config: {CONFIG_PATH}")
    print(f"[hb] endpoint: {endpoint}, node: {node_id}, interval: {interval}s")

    while True:
        payload = {
            "node_id": node_id,
            "version": "camnode-1.0.0",
            "hostname": host,
            "free_space_pct": free_space_pct(storage_base),
            "queue_len": queue_len(),
        }
        print(f"[hb] POST {endpoint} payload={payload}")
        status, body = post_heartbeat(endpoint, auth_token, payload)
        if status == 200 or status == 204:
            print(f"[hb] status={status} ok")
        else:
            print(f"[hb] status={status} body={body}")
        time.sleep(interval)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
