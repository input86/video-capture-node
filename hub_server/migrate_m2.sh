#!/usr/bin/env bash
set -euo pipefail

DB="/home/pi/data/hub.db"
TABLE="nodes"

# 1) Ensure base table exists (does nothing if it already does)
sqlite3 "$DB" "CREATE TABLE IF NOT EXISTS $TABLE (
  id TEXT PRIMARY KEY,
  name TEXT,
  created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);"

# Helper: check if a column exists
has_col() {
  local col="$1"
  sqlite3 "$DB" "PRAGMA table_info($TABLE);" | awk -F'|' '{print $2}' | grep -qx "$col"
}

# Helper: add a column if missing
add_col() {
  local col="$1" type="$2"
  if has_col "$col"; then
    echo "• Column $col already exists — skipping"
  else
    echo "• Adding column: $col $type"
    sqlite3 "$DB" "ALTER TABLE $TABLE ADD COLUMN $col $type;"
  fi
}

# 2) Add heartbeat-related columns only if missing
add_col "last_seen"       "TEXT"
add_col "ip"              "TEXT"
add_col "version"         "TEXT"
add_col "free_space_pct"  "REAL"
add_col "queue_len"       "INTEGER"

echo "Migration complete. Current columns:"
sqlite3 "$DB" "PRAGMA table_info($TABLE);"
