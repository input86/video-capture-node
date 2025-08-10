# RESTORE: Hub + Camera (M2 heartbeat stable)

This guide restores your working setup exactly as you have it now:

- **Hub Server**
  - API on **:5000** via `hub-api.service` (gunicorn, venv)
  - Heartbeat HTTP service on **:5050** via `hub-heartbeat.service` (Flask)
  - TFT UI via `tft-ui.service` (Tkinter, fullscreen)
  - Files under `/home/pi/data/clips/<node_id>/<YYYYMMDD>/…`
  - DB at `/home/pi/data/hub.db`
- **Camera Node**
  - Motion-triggered recorder + uploader via `camera-node.service`
  - Heartbeat via `camera-heartbeat.service`
  - Uses `/home/pi/camera_node/config.yaml` and venv
  - Uploads to the hub’s `/api/v1/clips` (port **5000**)
  - Heartbeats to hub (port **5050**)

> All paths, tokens, and thresholds match your M2 baseline.

---

## 0) Prereqs (both Pis)

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip sqlite3 curl ffmpeg
# (Hub w/ display only) Tk for TFT UI
sudo apt install -y python3-tk
