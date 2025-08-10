# /home/pi/hub_server/server.py
from flask import Flask, request, jsonify, render_template_string
import os
import sqlite3
import yaml
import datetime
import shutil
from werkzeug.utils import secure_filename

# -------- Config --------
cfg = yaml.safe_load(open("config.yaml"))

DB_PATH = cfg.get("database", "/home/pi/data/hub.db")
STORAGE_BASE = cfg.get("storage", {}).get("base_dir", "/home/pi/data")
MIN_FREE_PCT = cfg.get("storage", {}).get("min_free_percent", 10)
CLIPS_BASE = os.path.join(STORAGE_BASE, "clips")

os.makedirs(CLIPS_BASE, exist_ok=True)

app = Flask(__name__)

# -------- DB helpers --------
def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db_conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS nodes(
                node_id   TEXT PRIMARY KEY,
                last_seen TEXT,
                status    TEXT
            );
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS clips(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id   TEXT NOT NULL,
                filepath  TEXT NOT NULL,   -- relative to STORAGE_BASE (e.g. clips/cam01/...)
                timestamp TEXT NOT NULL
            );
        """)
        db.commit()

init_db()

# -------- Utilities --------
def _utcnow_iso():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")

def free_pct():
    total, used, free = shutil.disk_usage(STORAGE_BASE)
    return (free / total) * 100.0

def node_from_token(token):
    if not token:
        return None
    for node, t in cfg.get("auth_tokens", {}).items():
        if t == token:
            return node
    return None

def touch_node(node_id, status="online"):
    with db_conn() as db:
        db.execute(
            """
            INSERT INTO nodes(node_id, last_seen, status) VALUES (?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                status=excluded.status
            """,
            (node_id, _utcnow_iso(), status),
        )
        db.commit()

def fetch_nodes():
    with db_conn() as db:
        cur = db.execute("SELECT node_id, last_seen, status FROM nodes ORDER BY node_id;")
        return cur.fetchall()

# -------- API --------
@app.route("/api/v1/heartbeat", methods=["POST"])
def heartbeat():
    node = node_from_token(request.headers.get("X-Auth-Token"))
    if not node:
        return "Unauthorized", 401
    touch_node(node, "online")
    return jsonify({"ok": True, "free_percent": round(free_pct(), 2)}), 200

@app.route("/api/v1/clips", methods=["POST"])
def ingest_clip():
    node = node_from_token(request.headers.get("X-Auth-Token"))
    if not node:
        return "Unauthorized", 401

    # storage guard
    if free_pct() < float(MIN_FREE_PCT):
        touch_node(node, "low_storage")
        return jsonify({"error": "Insufficient storage"}), 507

    f = request.files.get("file")
    if not f:
        return "No file", 400

    fname = secure_filename(f.filename)
    date_str = datetime.datetime.utcnow().strftime("%Y%m%d")

    # Save under /home/pi/data/clips/<node>/<YYYYMMDD>/<fname>
    dst_dir = os.path.join(CLIPS_BASE, node, date_str)
    os.makedirs(dst_dir, exist_ok=True)

    abs_path = os.path.join(dst_dir, fname)
    f.save(abs_path)

    # Store RELATIVE path in DB (relative to STORAGE_BASE)
    rel_path = os.path.relpath(abs_path, STORAGE_BASE)

    with db_conn() as db:
        db.execute(
            "INSERT INTO clips(node_id, filepath, timestamp) VALUES (?,?,?)",
            (node, rel_path, _utcnow_iso()),
        )
        db.commit()

    touch_node(node, "online")
    return jsonify({"ok": True}), 200

# -------- Minimal status page --------
@app.route("/", methods=["GET"])
def index():
    pct = free_pct()
    nodes = fetch_nodes()
    html = """
    <html>
    <head>
        <title>Hub Server Status</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
            :root { color-scheme: dark; }
            body { background:#000; color:#fff; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin:0; }
            header { text-align:center; padding:12px 8px; font-size:22px; font-weight:700; }
            .status { text-align:center; font-size:16px; padding:8px 0; }
            .ok { color:#00d084; }
            .warn { color:#ff595e; }
            .list { padding:10px 14px; }
            .node { font-size:16px; padding:8px 0; border-bottom:1px solid #222; }
            .badge { display:inline-block; min-width:80px; padding:2px 8px; border-radius:6px; text-align:center; margin-right:8px; }
            .online { background:#0a2a12; color:#6cff9f; }
            .offline { background:#2a0a0a; color:#ff8a8a; }
            .muted { color:#aaa; }
            .footer { text-align:center; padding:10px; font-size:12px; color:#888; }
        </style>
    </head>
    <body>
        <header>Hub Server Status</header>
        <div class="status {{ 'warn' if pct < min_free else 'ok' }}">
            {{ 'LOW STORAGE' if pct < min_free else 'Storage OK' }} — {{ "%.1f" % pct }}% free
        </div>
        <div class="list">
            {% if not nodes %}
                <div class="node muted">No nodes registered yet…</div>
            {% else %}
                {% for row in nodes %}
                    {% set node = row['node_id'] %}
                    {% set last_seen = row['last_seen'] %}
                    {% set status = row['status'] %}
                    {% set is_online = (last_seen and last_seen != '') %}
                    <div class="node">
                        <span class="badge {{ 'online' if is_online else 'offline' }}">{{ 'ONLINE' if is_online else 'OFFLINE' }}</span>
                        <strong>{{ node }}</strong>
                        <span class="muted">— {{ status or '—' }} @ {{ last_seen or '—' }}</span>
                    </div>
                {% endfor %}
            {% endif %}
        </div>
        <div class="footer">/api/v1/heartbeat · /api/v1/clips</div>
    </body>
    </html>
    """
    return render_template_string(html, pct=pct, min_free=float(MIN_FREE_PCT), nodes=nodes)

# -------- Dev entrypoint --------
if __name__ == "__main__":
    # For ad-hoc testing only; systemd/gunicorn runs this in production.
    app.run(host="0.0.0.0", port=5000)
