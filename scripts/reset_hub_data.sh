#!/bin/bash
# reset_hub_data.sh
# Safely reset /home/pi/data on the hub to a clean state:
#  - Backs up existing /home/pi/data to /home/pi/data_backup_<timestamp>.tar.gz
#  - Asks for confirmation
#  - Clears clips directory
#  - Recreates hub.db with the current M2 schema

set -euo pipefail

HUB_HOME="/home/pi"
DATA_DIR="$HUB_HOME/data"
CLIPS_DIR="$DATA_DIR/clips"
DB_PATH="$DATA_DIR/hub.db"
BACKUP="/home/pi/data_backup_$(date +%Y%m%d_%H%M%S).tar.gz"

echo "[RESET] This will create a backup and then reset the hub data to a clean state."
echo "        Data dir: $DATA_DIR"
echo "        Backup:   $BACKUP"
echo
read -rp "Type YES to continue: " CONFIRM
if [[ "$CONFIRM" != "YES" ]]; then
  echo "[RESET] Cancelled."
  exit 1
fi

echo "[RESET] Stopping hub services..."
sudo systemctl stop hub-api || true
sudo systemctl stop hub-heartbeat || true
sudo systemctl stop tft-ui || true

echo "[RESET] Ensuring data directories exist..."
mkdir -p "$CLIPS_DIR"

echo "[RESET] Creating backup archive (this may take a moment)..."
tar -C "$HUB_HOME" -czf "$BACKUP" "$(basename "$DATA_DIR")"
echo "[RESET] Backup written to: $BACKUP"

echo "[RESET] Clearing clips directory..."
find "$CLIPS_DIR" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
find "$CLIPS_DIR" -type f -delete

echo "[RESET] Recreating hub.db schema..."
rm -f "$DB_PATH"
sqlite3 "$DB_PATH" <<'SQL'
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Nodes table (M2)
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  last_seen TEXT,
  status TEXT,
  ip TEXT,
  version TEXT,
  free_space_pct REAL,
  queue_len INTEGER
);

-- Clips table (M2)
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT,
  filepath TEXT,
  timestamp TEXT
);

-- helpful index for queries by node/timestamp
CREATE INDEX IF NOT EXISTS idx_clips_node_ts ON clips(node_id, timestamp);
SQL

echo "[RESET] Setting ownership..."
sudo chown -R pi:pi "$DATA_DIR"

echo "[RESET] Starting hub services..."
sudo systemctl start hub-heartbeat
sudo systemctl start hub-api
sudo systemctl start tft-ui

echo "[RESET] Done. Clean state is ready."
echo "        Use:  sqlite3 $DB_PATH 'SELECT * FROM nodes;'"
echo "        Files: $CLIPS_DIR (empty)"
