#!/usr/bin/env python3
import os, sqlite3, time, re
from pathlib import Path
from typing import Tuple, Optional, List

DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", "/home/pi/data"))
DB_PATH  = DATA_DIR / "hub.db"
CLIPS_DIR = DATA_DIR / "clips"

def _find_pk_column(cur, table: str) -> Optional[str]:
    cur.execute(f"PRAGMA table_info('{table}')")
    pk = None
    for cid, name, ctype, notnull, dflt, ispk in cur.fetchall():
        if ispk == 1:
            pk = name
            break
    return pk

def _find_path_column(cur, table: str) -> Optional[str]:
    # look for obvious path column names
    candidates = {"path", "filepath", "file_path", "clip_path"}
    cur.execute(f"PRAGMA table_info('{table}')")
    cols = [row[1] for row in cur.fetchall()]
    for c in cols:
        if c.lower() in candidates:
            return c
    # last resort: any TEXT column containing 'path'
    for c in cols:
        if "path" in c.lower():
            return c
    return None

def prune_db() -> Tuple[int, str]:
    """Remove rows in clips where file path no longer exists."""
    if not DB_PATH.exists():
        return 0, f"DB not found: {DB_PATH}"
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # ensure table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clips'")
        if not cur.fetchone():
            return 0, "Table 'clips' not found"

        pk = _find_pk_column(cur, "clips")
        pathcol = _find_path_column(cur, "clips")
        if not (pk and pathcol):
            return 0, f"Could not detect primary key or path column (pk={pk}, path={pathcol})"

        cur.execute(f"SELECT {pk}, {pathcol} FROM clips")
        rows = cur.fetchall()

        to_delete: List[int] = []
        for rid, p in rows:
            if not p:
                to_delete.append(rid)
                continue
            if not Path(p).exists():
                to_delete.append(rid)

        deleted = 0
        if to_delete:
            qmarks = ",".join("?" for _ in to_delete)
            cur.execute(f"DELETE FROM clips WHERE {pk} IN ({qmarks})", to_delete)
            deleted = cur.rowcount
        conn.commit()
        return deleted, f"Pruned {deleted} orphan rows (scanned {len(rows)})."
    finally:
        conn.close()

def clean_all_files() -> Tuple[int, str]:
    """Delete all files under /home/pi/data/clips (but not the DB)."""
    if not CLIPS_DIR.exists():
        return 0, f"Clips directory not found: {CLIPS_DIR}"
    deleted = 0
    for p in CLIPS_DIR.rglob("*"):
        try:
            if p.is_file():
                p.unlink()
                deleted += 1
        except Exception:
            pass
    return deleted, f"Deleted {deleted} files under {CLIPS_DIR}"

def reindex() -> Tuple[int, str]:
    """
    Rebuild 'clips' table entries based on the filesystem.
    We only insert rows if we can detect a 'path' column and a PK exists.
    We DO NOT drop the table; we insert missing records.
    """
    if not DB_PATH.exists():
        return 0, f"DB not found: {DB_PATH}"
    if not CLIPS_DIR.exists():
        return 0, f"Clips directory not found: {CLIPS_DIR}"
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clips'")
        if not cur.fetchone():
            return 0, "Table 'clips' not found"

        pk = _find_pk_column(cur, "clips")
        pathcol = _find_path_column(cur, "clips")
        if not pathcol:
            return 0, f"Could not detect path column."

        # Get existing paths
        cur.execute(f"SELECT {pathcol} FROM clips")
        existing = set(r[0] for r in cur.fetchall() if r and r[0])

        to_insert = []
        for mp4 in CLIPS_DIR.rglob("*.mp4"):
            p = str(mp4)
            if p not in existing:
                to_insert.append(p)

        inserted = 0
        if to_insert:
            # Try to insert minimal rows: only 'path' if possible
            # Find any other NOT NULL columns without default -> we can't safely fill those.
            cur.execute(f"PRAGMA table_info('clips')")
            cols = cur.fetchall()
            colnames = [c[1] for c in cols]
            notnull_no_default = [c for c in cols if c[3] == 1 and c[4] is None and c[1].lower() != pathcol.lower()]
            if notnull_no_default:
                # Can't safely insert missing required columns; bail with info
                return 0, ("Found required columns without defaults in 'clips'; "
                           "cannot auto-insert safely. Try prune_db, not reindex.")
            # Build and execute inserts
            cur.executemany(f"INSERT INTO clips ({pathcol}) VALUES (?)", [(p,) for p in to_insert])
            inserted = cur.rowcount
            conn.commit()
        return inserted, f"Inserted {inserted} missing rows. Scanned {len(to_insert)} candidates."
    finally:
        conn.close()

def stats() -> str:
    return f"DATA_DIR={DATA_DIR} DB_PATH={DB_PATH} CLIPS_DIR={CLIPS_DIR}"
