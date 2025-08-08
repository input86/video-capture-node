import shutil, yaml, sqlite3, time, os
from datetime import datetime

cfg = yaml.safe_load(open("config.yaml"))

def free_pct():
    total, used, free = shutil.disk_usage(cfg['storage']['base_dir'])
    return free / total * 100.0

def db_conn():
    return sqlite3.connect(cfg['database'])

def update_storage_status(status):
    with db_conn() as db:
        db.execute("""
            INSERT INTO nodes(node_id, last_seen, status)
            VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE
            SET last_seen=excluded.last_seen, status=excluded.status
        """, ("hub_server", datetime.utcnow().isoformat(timespec="seconds"), status))
        db.commit()

if __name__ == "__main__":
    os.makedirs(cfg['storage']['base_dir'], exist_ok=True)
    while True:
        pct = free_pct()
        if pct < cfg['storage']['min_free_percent']:
            update_storage_status(f"LOW_STORAGE ({pct:.1f}%)")
        else:
            update_storage_status(f"OK ({pct:.1f}%)")
        time.sleep(5)
