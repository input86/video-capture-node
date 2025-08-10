#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import yaml
import sqlite3
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timezone
import shutil
import os
import re
from typing import Optional, Dict, List, Tuple, Set

# -------- Config --------
cfg = yaml.safe_load(open("config.yaml", "r"))

DB_PATH = cfg.get("database", "/home/pi/data/hub.db")
STORAGE_BASE = cfg.get("storage", {}).get("base_dir", "/home/pi")
CLIPS_BASE = os.path.join(STORAGE_BASE, "clips")
MIN_FREE_PCT = float(cfg.get("storage", {}).get("min_free_percent", 10))

# Heartbeat thresholds (seconds)
HEARTBEAT_ONLINE_SEC = 10
HEARTBEAT_STALE_SEC = 30

# -------- DB helpers --------
def db_conn():
    return sqlite3.connect(DB_PATH)

def table_cols(table: str) -> List[str]:
    with db_conn() as db:
        cur = db.cursor()
        cur.execute(f"PRAGMA table_info({table});")
        return [r[1] for r in cur.fetchall()]  # name is index 1

def build_nodes_select() -> Tuple[str, List[str]]:
    """
    Returns (sql, out_cols) where out_cols are:
      ['node_id','last_seen','ip','version','free_space_pct','queue_len','legacy_status']
    The SELECT only references existing columns; missing ones are returned as NULL aliases.
    """
    cols = set(table_cols("nodes"))

    # choose identifier
    if "node_id" in cols:
        id_expr = "node_id AS node_id"
    elif "id" in cols:
        id_expr = "id AS node_id"
    else:
        # No usable id column—return a query that yields zero rows but correct shape
        sql = ("SELECT NULL AS node_id, NULL AS last_seen, NULL AS ip, NULL AS version, "
               "NULL AS free_space_pct, NULL AS queue_len, NULL AS legacy_status WHERE 0")
        return sql, ["node_id","last_seen","ip","version","free_space_pct","queue_len","legacy_status"]

    last_seen_expr = "last_seen AS last_seen" if "last_seen" in cols else "NULL AS last_seen"
    ip_expr        = "ip AS ip"               if "ip" in cols        else "NULL AS ip"
    ver_expr       = "version AS version"     if "version" in cols   else "NULL AS version"
    free_expr      = "free_space_pct AS free_space_pct" if "free_space_pct" in cols else "NULL AS free_space_pct"
    queue_expr     = "queue_len AS queue_len" if "queue_len" in cols else "NULL AS queue_len"
    legacy_expr    = "status AS legacy_status" if "status" in cols   else "NULL AS legacy_status"

    sql = f"""
        SELECT
          {id_expr},
          {last_seen_expr},
          {ip_expr},
          {ver_expr},
          {free_expr},
          {queue_expr},
          {legacy_expr}
        FROM nodes
        ORDER BY 1
    """.strip()

    return sql, ["node_id","last_seen","ip","version","free_space_pct","queue_len","legacy_status"]

def fetch_nodes() -> List[Dict]:
    sql, out_cols = build_nodes_select()
    with db_conn() as db:
        cur = db.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = {out_cols[i]: r[i] for i in range(len(out_cols))}
        out.append(d)
    return out

# -------- Storage --------
def free_pct() -> float:
    total, used, free = shutil.disk_usage(STORAGE_BASE)
    return (free / total * 100.0) if total else 0.0

# -------- Status helpers --------
def parse_iso(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        if isinstance(iso_str, bytes):
            iso_str = iso_str.decode("utf-8", "ignore")
        s = str(iso_str)
        if s.endswith("Z"):
            try:
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def computed_status(last_seen_iso: Optional[str]) -> str:
    dt = parse_iso(last_seen_iso)
    if not dt:
        return "offline"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta <= HEARTBEAT_ONLINE_SEC:
        return "online"
    if delta <= HEARTBEAT_STALE_SEC:
        return "stale"
    return "offline"

def status_color(status: str) -> str:
    if status == "online":
        return "#16a34a"  # green
    if status == "stale":
        return "#f59e0b"  # yellow
    return "#dc2626"      # red

# -------- Clip utilities (reindex/prune) --------
FN_TS_RE = re.compile(r".*?(\d{8}T\d{6})Z", re.I)

def ts_from_filename(fn: str) -> Optional[str]:
    """
    Extract ISO timestamp (UTC Z) from filename like ..._20250809T024522Z.mp4
    Returns ISO "YYYY-MM-DDTHH:MM:SSZ" or None.
    """
    m = FN_TS_RE.match(fn)
    if not m:
        return None
    raw = m.group(1)  # YYYYMMDDTHHMMSS
    try:
        dt = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None

def load_existing_paths() -> Tuple[str, Set[str]]:
    """
    Returns (mode, existing_paths)
    mode is 'legacy' if table has 'filepath', else 'new' if it has 'rel_path'
    existing_paths contains normalized keys for quick duplicate checks.
    """
    cols = set(table_cols("clips"))
    if "filepath" in cols:
        with db_conn() as db:
            cur = db.cursor()
            cur.execute("SELECT filepath FROM clips")
            return "legacy", {row[0] for row in cur.fetchall() if row and row[0]}
    elif "rel_path" in cols:
        with db_conn() as db:
            cur = db.cursor()
            cur.execute("SELECT rel_path FROM clips")
            return "new", {row[0] for row in cur.fetchall() if row and row[0]}
    else:
        return "unknown", set()

def prune_db():
    """Remove rows from clips whose file is missing on disk."""
    removed = 0
    cols = set(table_cols("clips"))
    path_col = "filepath" if "filepath" in cols else ("rel_path" if "rel_path" in cols else None)
    if not path_col:
        toast(f"clips table missing filepath/rel_path", "orange")
        return
    with db_conn() as db:
        cur = db.cursor()
        cur.execute(f"SELECT id, {path_col} FROM clips")
        for clip_id, p in cur.fetchall():
            if not p:
                continue
            abs_p = p if path_col == "filepath" else os.path.join(STORAGE_BASE, p.lstrip("/"))
            if not os.path.exists(abs_p):
                db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
                removed += 1
        db.commit()
    toast(f"Pruned {removed} records", "lime")

def reindex_db():
    """
    Walk CLIPS_BASE and insert any missing files into clips.
    Works with legacy and new schema.
    """
    if not os.path.isdir(CLIPS_BASE):
        toast("No clips directory found", "orange")
        return

    mode, existing = load_existing_paths()
    if mode == "unknown":
        toast("clips table not recognized", "red")
        return

    inserted = 0
    errors = 0
    for node_id in sorted(os.listdir(CLIPS_BASE)):
        node_dir = os.path.join(CLIPS_BASE, node_id)
        if not os.path.isdir(node_dir):
            continue
        for ymd in sorted(os.listdir(node_dir)):
            day_dir = os.path.join(node_dir, ymd)
            if not os.path.isdir(day_dir):
                continue
            for fn in sorted(os.listdir(day_dir)):
                if not fn.lower().endswith(".mp4"):
                    continue
                abs_path = os.path.join(day_dir, fn)
                rel_path = os.path.relpath(abs_path, STORAGE_BASE)  # e.g. clips/cam01/20250809/file.mp4

                key = abs_path if mode == "legacy" else rel_path
                if key in existing:
                    continue

                # derive timestamp
                ts = ts_from_filename(fn)
                if not ts:
                    # fallback: file mtime
                    try:
                        mtime = os.path.getmtime(abs_path)
                        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                        ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception:
                        ts = None

                try:
                    with db_conn() as db:
                        cur = db.cursor()
                        if mode == "legacy":
                            cur.execute(
                                "INSERT INTO clips (node_id, filepath, timestamp) VALUES (?,?,?)",
                                (node_id, abs_path, ts)
                            )
                        else:
                            size_b = None
                            try:
                                size_b = os.path.getsize(abs_path)
                            except Exception:
                                pass
                            cur.execute(
                                "INSERT INTO clips (node_id, rel_path, created_at, bytes, status) VALUES (?,?,?,?,?)",
                                (node_id, rel_path, ts, size_b, "stored")
                            )
                        db.commit()
                        inserted += 1
                        existing.add(key)
                except Exception:
                    errors += 1

    color = "lime" if errors == 0 else "#f59e0b"
    toast(f"Reindex complete: {inserted} added, {errors} errors", color)

def clean_all_files():
    """Confirm, then delete all clip files and clear clips table."""
    if not messagebox.askyesno("Confirm", "Delete ALL clip files and clear the clips DB table?"):
        toast("Clean canceled", "#bbbbbb")
        return

    # Delete files
    deleted = 0
    for rootdir, dirs, files in os.walk(CLIPS_BASE):
        for f in files:
            try:
                os.remove(os.path.join(rootdir, f))
                deleted += 1
            except Exception:
                pass

    # Clear DB
    try:
        with db_conn() as db:
            db.execute("DELETE FROM clips")
            db.commit()
    except Exception as e:
        toast(f"DB clean error: {e}", "red")
        return

    toast(f"Deleted {deleted} files; DB cleared", "lime")

# -------- UI --------
root = tk.Tk()
root.attributes("-fullscreen", True)
root.configure(bg="black")

def toast(msg: str, color: str = "white", timeout_ms: int = 2500):
    toast_label.config(text=msg, fg=color)
    if timeout_ms:
        root.after(timeout_ms, lambda: toast_label.config(text=""))

# Top area
title = tk.Label(root, text="Hub Server Status", font=("Arial", 24, "bold"), fg="white", bg="black")
title.pack(pady=(6,2))

toast_label = tk.Label(root, text="", font=("Arial", 12), fg="white", bg="black")
toast_label.pack(pady=(0,4))

storage_label = tk.Label(root, text="", font=("Arial", 16), fg="white", bg="black")
storage_label.pack(pady=(0,6))

# Node list area
frame = tk.Frame(root, bg="black")
frame.pack(fill="both", expand=True, padx=10, pady=6)

# Controls (3 buttons)
controls = tk.Frame(root, bg="black")
controls.pack(fill="x", padx=10, pady=(2,6))

btn_prune = tk.Button(
    controls, text="Prune DB", font=("Arial", 14, "bold"),
    fg="white", bg="#2563eb", activebackground="#1e40af",
    padx=12, pady=6, command=prune_db
)
btn_prune.pack(side="left", padx=(0,8))

btn_reindex = tk.Button(
    controls, text="Reindex DB", font=("Arial", 14, "bold"),
    fg="white", bg="#0ea5e9", activebackground="#0284c7",
    padx=12, pady=6, command=reindex_db
)
btn_reindex.pack(side="left", padx=(0,8))

btn_clean = tk.Button(
    controls, text="Clean All Files…", font=("Arial", 14, "bold"),
    fg="white", bg="#dc2626", activebackground="#991b1b",
    padx=12, pady=6, command=clean_all_files
)
btn_clean.pack(side="left")

footer = tk.Label(root, text="/api/v1/heartbeat · /api/v1/clips", font=("Arial", 12), fg="#888", bg="black")
footer.pack(pady=(0,6))

def render_node_row(parent, node: Dict):
    node_id = node.get("node_id") or "—"
    last_seen = node.get("last_seen")
    ip = node.get("ip")
    version = node.get("version")
    free_space_pct = node.get("free_space_pct")
    queue_len = node.get("queue_len")

    stat = computed_status(last_seen)
    color = status_color(stat)

    row = tk.Frame(parent, bg="black")
    row.pack(anchor="w", fill="x", padx=2, pady=3)

    top_line = tk.Frame(row, bg="black")
    top_line.pack(anchor="w", fill="x")

    id_label = tk.Label(top_line, text=f"{node_id}", font=("Arial", 18, "bold"), fg="white", bg="black")
    id_label.pack(side="left", padx=(0, 10))

    chip = tk.Label(top_line, text=stat.upper(), font=("Arial", 12, "bold"),
                    fg="white", bg=color, padx=8, pady=2)
    chip.pack(side="left")

    details = []
    details.append(f"Last: {last_seen or '—'}")
    if ip: details.append(f"IP: {ip}")
    if version: details.append(f"Ver: {version}")
    try:
        if isinstance(free_space_pct, (int, float)):
            details.append(f"Free%: {float(free_space_pct):.1f}")
    except Exception:
        pass
    try:
        if isinstance(queue_len, int):
            details.append(f"Queue: {queue_len}")
    except Exception:
        pass

    det_label = tk.Label(row, text="   ·   ".join(details), font=("Arial", 12), fg="#bbbbbb", bg="black",
                         wraplength=460, justify="left")
    det_label.pack(anchor="w", pady=(3,0))

def refresh():
    # Storage banner
    pct = free_pct()
    if pct < MIN_FREE_PCT:
        storage_label.config(text=f"⚠ LOW STORAGE: {pct:.1f}% free", fg="red")
    else:
        storage_label.config(text=f"Storage OK: {pct:.1f}% free", fg="lime")

    # Clear list
    for w in frame.winfo_children():
        w.destroy()

    # Nodes
    try:
        rows = fetch_nodes()
    except Exception as e:
        tk.Label(frame, text=f"DB error: {e}", font=("Arial", 16), fg="red", bg="black").pack(anchor="w")
        root.after(2000, refresh)
        return

    if not rows:
        tk.Label(frame, text="No nodes registered yet…", font=("Arial", 16), fg="gray", bg="black").pack(anchor="w")
    else:
        for node in rows:
            render_node_row(frame, node)

    root.after(2000, refresh)

# Allow exiting with ESC if you’re testing without the service
def on_key(event):
    if event.keysym == "Escape":
        root.destroy()
root.bind("<Key>", on_key)

refresh()
root.mainloop()
