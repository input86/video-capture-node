#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import yaml, sqlite3, json, urllib.request, urllib.error
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timezone
import shutil, os, textwrap, threading
from typing import Optional, Dict, List, Tuple

# -------- Config --------
cfg = yaml.safe_load(open("config.yaml", "r"))

DB_PATH = cfg.get("database", "/home/pi/data/hub.db")
STORAGE_BASE = cfg.get("storage", {}).get("base_dir", "/home/pi/data/clips")
MIN_FREE_PCT = float(cfg.get("storage", {}).get("min_free_percent", 10))
API_URL = "http://127.0.0.1:5050/api/v1/nodes"

# Heartbeat thresholds (seconds) — import from heartbeat_config if present
HEARTBEAT_ONLINE_SEC = 10
HEARTBEAT_STALE_SEC = 30
try:
    import heartbeat_config as hbconf  # lives next to heartbeatd.py
    HEARTBEAT_ONLINE_SEC = int(getattr(hbconf, "HEARTBEAT_ONLINE_SEC", HEARTBEAT_ONLINE_SEC))
    HEARTBEAT_STALE_SEC  = int(getattr(hbconf, "HEARTBEAT_STALE_SEC", HEARTBEAT_STALE_SEC))
except Exception:
    pass

# Fonts/Sizing tuned to small TFTs
FONT_TITLE = ("Arial", 22, "bold")
FONT_BANNER = ("Arial", 16)
FONT_NODE_ID = ("Arial", 16, "bold")
FONT_CHIP = ("Arial", 12, "bold")
FONT_DETAILS = ("Arial", 12)
FONT_FOOTER = ("Arial", 11)
FONT_BTN = ("Arial", 11, "bold")

ROW_PADY = 4
ROW_PADX = 6
MAX_DETAIL_WIDTH = 62  # wrap point for details line (tweak for your TFT)

# -------- DB helpers --------
def db_conn():
    return sqlite3.connect(DB_PATH)

def table_cols(table: str) -> List[str]:
    with db_conn() as db:
        cur = db.cursor()
        cur.execute(f"PRAGMA table_info({table});")
        return [r[1] for r in cur.fetchall()]

def build_nodes_select() -> Tuple[str, List[str]]:
    cols = set(table_cols("nodes"))

    if "node_id" in cols:
        id_expr = "node_id AS node_id"
    elif "id" in cols:
        id_expr = "id AS node_id"
    else:
        sql = "SELECT NULL AS node_id, NULL AS last_seen, NULL AS ip, NULL AS version, NULL AS free_space_pct, NULL AS queue_len, NULL AS legacy_status WHERE 0"
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

def fetch_nodes_from_db() -> List[Dict]:
    sql, out_cols = build_nodes_select()
    with db_conn() as db:
        cur = db.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = {out_cols[i]: r[i] for i in range(len(out_cols))}
        out.append(d)
    # compute status from last_seen if hub API is unavailable
    for d in out:
        d["status"] = computed_status(d.get("last_seen"))
    return out

def fetch_nodes_from_api() -> Optional[List[Dict]]:
    try:
        req = urllib.request.Request(API_URL, headers={"Cache-Control": "no-store"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
            nodes = data.get("nodes") or []
            # Ensure node_id key exists for sorting
            for n in nodes:
                if "node_id" not in n and "id" in n:
                    n["node_id"] = n["id"]
            return nodes
    except Exception:
        return None

# ---- clips table helpers (schema-agnostic id + path) ----
def clips_id_and_path_rows() -> List[Tuple[int, str]]:
    """
    Returns list of (id, rel_path) from the 'clips' table,
    mapping common column names.
    """
    with db_conn() as db:
        cur = db.cursor()
        cols = {c.lower() for c in table_cols("clips")}
        # Determine path column name
        path_col = None
        for cand in ("filepath", "rel_path", "path"):
            if cand in cols:
                path_col = cand
                break
        if not path_col:  # last resort, attempt to infer
            # return empty list if we can't find a path column
            return []
        # Determine id column
        id_col = "id" if "id" in cols else next((c for c in ("clip_id","rowid") if c in cols), "rowid")
        sql = f'SELECT {id_col} AS id, {path_col} AS rel_path FROM clips ORDER BY id;'
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            return [(int(r[0]), str(r[1])) for r in rows if r and r[1] is not None]
        except Exception:
            return []

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
    return {"online": "#16a34a", "stale": "#f59e0b"}.get(status, "#dc2626")

# -------- UI scaffolding (scrollable) --------
root = tk.Tk()
root.attributes("-fullscreen", True)
root.configure(bg="black")

# Toolbar
toolbar = tk.Frame(root, bg="black")
toolbar.pack(fill="x", padx=8, pady=(6,2))

title = tk.Label(toolbar, text="Hub Server Status", font=FONT_TITLE, fg="white", bg="black")
title.pack(side="left")

btns = tk.Frame(toolbar, bg="black")
btns.pack(side="right")

prune_btn = ttk.Button(btns, text="Prune DB", width=10)
clean_btn = ttk.Button(btns, text="Clean Files…", width=12)
prune_btn.pack(side="left", padx=(6,0))
clean_btn.pack(side="left", padx=(6,0))

storage_label = tk.Label(root, text="", font=FONT_BANNER, fg="white", bg="black")
storage_label.pack(pady=2)

# Scrollable area for node list
container = tk.Frame(root, bg="black")
container.pack(fill="both", expand=True, padx=8, pady=4)

canvas = tk.Canvas(container, bg="black", highlightthickness=0)
scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
list_frame = tk.Frame(canvas, bg="black")

list_frame.bind(
    "<Configure>",
    lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
)
canvas.create_window((0, 0), window=list_frame, anchor="nw")
canvas.configure(yscrollcommand=scrollbar.set)

canvas.pack(side="left", fill="both", expand=True)
scrollbar.pack(side="right", fill="y")

footer = tk.Label(root, text="API: /api/v1/heartbeat · /api/v1/clips", font=FONT_FOOTER, fg="#888", bg="black")
footer.pack(pady=(2,0))

updated_label = tk.Label(root, text="", font=FONT_FOOTER, fg="#888", bg="black")
updated_label.pack(pady=(0,4))

status_msg = tk.Label(root, text="", font=FONT_FOOTER, fg="#9ca3af", bg="black")
status_msg.pack(pady=(0,6))

def set_status(msg: str):
    status_msg.config(text=msg)

def wrap_details(s: str) -> str:
    return "\n".join(textwrap.wrap(s, width=MAX_DETAIL_WIDTH, break_long_words=False, replace_whitespace=False))

def render_node_row(parent, node: Dict):
    node_id = (node.get("node_id") or "—")
    last_seen = node.get("last_seen")
    ip = node.get("ip")
    version = node.get("version")
    free_space_pct = node.get("free_space_pct")
    queue_len = node.get("queue_len")
    status = (node.get("status") or computed_status(last_seen))

    color = status_color(status)

    row = tk.Frame(parent, bg="black")
    row.pack(anchor="w", fill="x", padx=ROW_PADX, pady=ROW_PADY)

    top = tk.Frame(row, bg="black")
    top.pack(anchor="w", fill="x")

    id_label = tk.Label(top, text=f"{node_id}", font=FONT_NODE_ID, fg="white", bg="black")
    id_label.pack(side="left")

    chip = tk.Label(top, text=status.upper(), font=FONT_CHIP, fg="white", bg=color, padx=8, pady=1)
    chip.pack(side="left", padx=(8,0))

    details = [f"Last: {last_seen or '—'}"]
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

    details_str = "   ·   ".join(details)
    details_wrapped = wrap_details(details_str)

    det_label = tk.Label(row, text=details_wrapped, font=FONT_DETAILS, fg="#bbbbbb", bg="black", justify="left")
    det_label.pack(anchor="w", pady=(3, 0))

def classify_status(s: str) -> int:
    return {"online": 0, "stale": 1, "offline": 2}.get(s, 3)

def refresh():
    # Storage banner
    pct = free_pct()
    if pct < MIN_FREE_PCT:
        storage_label.config(text=f"⚠ LOW STORAGE: {pct:.1f}% free", fg="red")
    else:
        storage_label.config(text=f"Storage OK: {pct:.1f}% free", fg="lime")

    # Clear the list_frame
    for w in list_frame.winfo_children():
        w.destroy()

    # Prefer API → fallback to DB
    nodes = fetch_nodes_from_api()
    used_api = True
    if nodes is None:
        nodes = fetch_nodes_from_db()
        used_api = False

    if not nodes:
        tk.Label(list_frame, text="No nodes registered yet…", font=FONT_DETAILS, fg="gray", bg="black").pack(anchor="w", padx=ROW_PADX, pady=ROW_PADY)
    else:
        norm: List[Dict] = []
        for n in nodes:
            node_id = n.get("node_id") or n.get("id") or "—"
            status = (n.get("status") or computed_status(n.get("last_seen")))
            norm.append({
                "node_id": node_id,
                "status": status,
                "last_seen": n.get("last_seen"),
                "ip": n.get("ip"),
                "version": n.get("version"),
                "free_space_pct": n.get("free_space_pct"),
                "queue_len": n.get("queue_len"),
            })
        norm.sort(key=lambda x: (classify_status(x["status"]), str(x["node_id"]).lower()))
        for node in norm:
            render_node_row(list_frame, node)

    # Update footer with source + time
    src = "API" if used_api else "DB"
    updated_label.config(text=f"Updated {datetime.now().strftime('%H:%M:%S')} · Source: {src}")

    # Keep viewport at top on each refresh (avoids half-cut rows on small TFTs)
    canvas.yview_moveto(0.0)

    root.after(2000, refresh)

# ---- Maintenance actions (run in background threads) ----
def with_buttons_disabled(func):
    def wrapper():
        prune_btn.config(state="disabled")
        clean_btn.config(state="disabled")
        try:
            func()
        finally:
            prune_btn.config(state="normal")
            clean_btn.config(state="normal")
    return wrapper

@with_buttons_disabled
def action_prune_db():
    set_status("Pruning DB…")
    removed = 0
    total = 0
    rows = clips_id_and_path_rows()
    if not rows:
        set_status("Prune: no clips or unknown schema.")
        return
    # Build full path and delete missing ones
    to_delete = []
    for cid, rel in rows:
        total += 1
        rel = rel.lstrip("/").replace("//", "/")
        fpath = os.path.join(STORAGE_BASE, rel)
        if not os.path.exists(fpath):
            to_delete.append(cid)
    if to_delete:
        with db_conn() as db:
            cur = db.cursor()
            for cid in to_delete:
                cur.execute("DELETE FROM clips WHERE id = ?;", (cid,))
            db.commit()
        removed = len(to_delete)
    set_status(f"Prune done: removed {removed} / {total} rows with missing files.")

@with_buttons_disabled
def action_clean_files():
    # Confirm
    if not messagebox.askyesno("Confirm", "Delete ALL clip files and clear the DB?\nThis cannot be undone.", default=messagebox.NO, icon=messagebox.WARNING):
        set_status("Clean canceled.")
        return
    set_status("Cleaning files and DB… this may take a moment.")
    # Delete *.mp4 under STORAGE_BASE
    deleted = 0
    for rootdir, dirs, files in os.walk(STORAGE_BASE):
        for name in files:
            if name.lower().endswith(".mp4"):
                try:
                    os.remove(os.path.join(rootdir, name))
                    deleted += 1
                except Exception:
                    pass
    # Remove empty date directories (optional, best-effort)
    for rootdir, dirs, files in os.walk(STORAGE_BASE, topdown=False):
        try:
            if not os.listdir(rootdir):
                os.rmdir(rootdir)
        except Exception:
            pass
    # Clear DB clips table
    try:
        with db_conn() as db:
            db.execute("DELETE FROM clips;")
            db.commit()
    except Exception:
        pass
    set_status(f"Clean complete: deleted {deleted} files; DB cleared.")

def on_prune_clicked():
    threading.Thread(target=action_prune_db, daemon=True).start()

def on_clean_clicked():
    threading.Thread(target=action_clean_files, daemon=True).start()

prune_btn.config(command=on_prune_clicked)
clean_btn.config(command=on_clean_clicked)

# Allow exiting with ESC for manual testing
def on_key(event):
    if event.keysym == "Escape":
        root.destroy()
root.bind("<Key>", on_key)

refresh()
root.mainloop()
