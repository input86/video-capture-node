#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import yaml
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent

def load_cfg():
    # Match the same config keys used by the rest of the hub
    cfg = yaml.safe_load(open(HERE / "config.yaml", "r"))
    db_path = cfg.get("database", "/home/pi/data/hub.db")
    base_dir = cfg.get("storage", {}).get("base_dir", "/home/pi/data")
    clips_dir = os.path.join(base_dir, "clips")
    return db_path, base_dir, clips_dir

def db_conn(db_path):
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA synchronous=NORMAL;")
    return db

FILENAME_TS = re.compile(r".*_(\d{8}T\d{6}Z)\.mp4$", re.IGNORECASE)

def file_ts_iso(fp: Path) -> str:
    """
    Try to parse timestamp from filename like cam01_20250809T024522Z.mp4.
    Fallback to file mtime in UTC (ISO Z).
    """
    m = FILENAME_TS.match(fp.name)
    if m:
        s = m.group(1)
        # Convert 20250809T024522Z -> 2025-08-09T02:45:22Z
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z"
    # fallback mtime
    dt = datetime.utcfromtimestamp(fp.stat().st_mtime).replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def infer_node_id(fp: Path, clips_root: Path) -> str:
    """
    Expect structure: <clips_root>/<node_id>/<YYYYMMDD>/<file>.mp4
    If not matched, return the parent folder name as a best-effort.
    """
    try:
        rel = fp.relative_to(clips_root)
        parts = rel.parts
        if len(parts) >= 3:
            return parts[0]  # node_id
        if len(parts) >= 1:
            return parts[0]
    except Exception:
        pass
    return fp.parent.name

def ensure_schema(db: sqlite3.Connection):
    # Keep schema consistent with your current hub (id, node_id, filepath, timestamp)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS clips (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      node_id TEXT,
      filepath TEXT,
      timestamp TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_clips_node_ts ON clips(node_id, timestamp);
    """)
    db.commit()

def clip_exists(db: sqlite3.Connection, filepath: str) -> bool:
    cur = db.execute("SELECT 1 FROM clips WHERE filepath=? LIMIT 1", (filepath,))
    return cur.fetchone() is not None

def main():
    db_path, base_dir, clips_dir = load_cfg()
    clips_root = Path(clips_dir)

    if not clips_root.exists():
        print(f"[reindex] clips dir not found: {clips_root}", file=sys.stderr)
        sys.exit(1)

    db = db_conn(db_path)
    ensure_schema(db)

    added = 0
    scanned = 0

    for fp in clips_root.rglob("*.mp4"):
        scanned += 1
        abs_path = str(fp.resolve())
        if clip_exists(db, abs_path):
            continue
        node_id = infer_node_id(fp, clips_root)
        ts = file_ts_iso(fp)
        db.execute("INSERT INTO clips(node_id, filepath, timestamp) VALUES(?,?,?)",
                   (node_id, abs_path, ts))
        added += 1
        if added % 50 == 0:
            db.commit()

    db.commit()
    print(f"[reindex] scanned={scanned}, added={added}, db={db_path}")

if __name__ == "__main__":
    main()
