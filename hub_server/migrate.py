import os, sqlite3, yaml
cfg = yaml.safe_load(open("config.yaml"))
os.makedirs(os.path.dirname(cfg['database']), exist_ok=True)

with sqlite3.connect(cfg['database']) as db:
    cur = db.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS nodes(
      node_id TEXT PRIMARY KEY,
      last_seen TEXT,
      status TEXT
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS clips(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      node_id TEXT,
      filepath TEXT,
      timestamp TEXT
    );""")
    db.commit()
print("Database initialized at", cfg['database'])
