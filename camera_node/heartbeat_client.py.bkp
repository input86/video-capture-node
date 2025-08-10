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

def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

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

    # Prefer microservice on 5050 if hub_url specifies :5000
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
