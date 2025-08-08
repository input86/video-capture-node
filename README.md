# RC Drift Highlight Capture System

A Raspberry Pi–powered system for capturing RC drift video highlights using multiple motion-triggered cameras and a centralized server.

## 📁 Project Structure

```
video-capture-node/
├── camera_node/        # For Pi Zero 2 W: handles sensor + camera + upload
├── hub_server/         # For Pi 4: receives clips, runs UI, manages storage
├── git-backup.sh       # Pushes local changes to GitHub
└── README.md
```

## 📷 Camera Node (Pi Zero 2 W)

### 🔧 Setup Instructions

1. Flash Raspberry Pi OS Lite
2. Configure static IP (e.g. 192.168.0.151)
3. Install system:

```bash
sudo apt update
sudo apt install -y git
git clone git@github.com:input86/video-capture-node.git
cd video-capture-node/camera_node
./installcamera.sh
```

4. Edit config.yaml:

```yaml
hub_url: "http://192.168.0.150:5000"  # Pi 4 IP
node_id: "cam01"                     # Unique per node
auth_token: "your_shared_secret"    # Must match hub_server config
```

## 🖥️ Hub Server (Pi 4)

### 🔧 Setup Instructions

1. Flash Raspberry Pi OS
2. Configure static IP (e.g. 192.168.0.150)
3. Install system:

```bash
sudo apt update
sudo apt install -y git
git clone git@github.com:input86/video-capture-node.git
cd video-capture-node/hub_server
./installserver.sh
```

4. Edit config.yaml:

```yaml
storage:
  base_dir: /home/pi/data/clips
  min_free_percent: 10

database: /home/pi/data/hub.db

auth_tokens:
  cam01: your_shared_secret
  cam02: another_secret
```

5. Check service:

```bash
sudo systemctl status hub-api.service
```

## 🔁 Backing Up to GitHub

Use the provided script to commit and push changes:

```bash
cd ~/video-capture-node
./git-backup.sh
```

## 🌲 Branching Strategy

Single `main` branch using folders:
- `camera_node/` = Pi Zero 2 W
- `hub_server/` = Pi 4

## 📜 License

MIT License (or your preferred terms)
