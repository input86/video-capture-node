import yaml, sqlite3, tkinter as tk
from datetime import datetime, timezone
import shutil, os

cfg = yaml.safe_load(open("config.yaml"))

def db_conn():
    return sqlite3.connect(cfg['database'])

def fetch_nodes():
    with db_conn() as db:
        cur = db.cursor()
        cur.execute("SELECT node_id, last_seen, status FROM nodes ORDER BY node_id")
        return cur.fetchall()

def free_pct():
    total, used, free = shutil.disk_usage(cfg['storage']['base_dir'])
    return free / total * 100.0

def is_online(last_seen_iso, timeout_sec=60):
    if not last_seen_iso:
        return False
    try:
        last = datetime.fromisoformat(last_seen_iso)
    except:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() < timeout_sec

root = tk.Tk()
root.attributes("-fullscreen", True)
root.configure(bg="black")

title = tk.Label(root, text="Hub Server Status", font=("Arial", 28, "bold"), fg="white", bg="black")
title.pack(pady=6)

storage_label = tk.Label(root, text="", font=("Arial", 20), fg="white", bg="black")
storage_label.pack(pady=2)

frame = tk.Frame(root, bg="black")
frame.pack(fill="both", expand=True, padx=12, pady=6)

footer = tk.Label(root, text="/api/v1/heartbeat · /api/v1/clips", font=("Arial", 12), fg="#888", bg="black")
footer.pack(pady=2)

def refresh():
    pct = free_pct()
    if pct < cfg['storage']['min_free_percent']:
        storage_label.config(text=f"⚠ LOW STORAGE: {pct:.1f}% free", fg="red")
    else:
        storage_label.config(text=f"Storage OK: {pct:.1f}% free", fg="lime")

    for w in frame.winfo_children():
        w.destroy()

    nodes = fetch_nodes()
    if not nodes:
        tk.Label(frame, text="No nodes registered yet…", font=("Arial", 18), fg="gray", bg="black").pack(anchor="w")
    else:
        for node, last_seen, status in nodes:
            online = is_online(last_seen)
            color = "lime" if online else "red"
            txt = f"{node}: {'Online' if online else 'Offline'} [{status or '—'}] @ {last_seen or '—'}"
            tk.Label(frame, text=txt, font=("Arial", 18), fg=color, bg="black").pack(anchor="w")

    root.after(2000, refresh)

refresh()
root.mainloop()
