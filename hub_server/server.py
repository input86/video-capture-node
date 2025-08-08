from flask import Flask, request, jsonify, render_template_string
import os, sqlite3, yaml, datetime, shutil
from werkzeug.utils import secure_filename

cfg = yaml.safe_load(open("config.yaml"))
app = Flask(__name__)
os.makedirs(cfg['storage']['base_dir'], exist_ok=True)

def db_conn():
    # sqlite: use short-lived connections per request
    return sqlite3.connect(cfg['database'])

def _utcnow_iso():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")

def free_pct():
    total, used, free = shutil.disk_usage(cfg['storage']['base_dir'])
    return free / total * 100.0

def node_from_token(token):
    if not token:
        return None
    for node, t in cfg['auth_tokens'].items():
        if t == token:
            return node
    return None

def touch_node(node_id, status="online"):
    with db_conn() as db:
        db.execute(
            "INSERT INTO nodes(node_id,last_seen,status) VALUES (?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET last_seen=excluded.last_seen, status=excluded.status",
            (node_id, _utcnow_iso(), status)
        )
        db.commit()

def fetch_nodes():
    with db_conn() as db:
        cur = db.cursor()
        cur.execute("SELECT node_id, last_seen, status FROM nodes ORDER BY node_id")
        return cur.fetchall()

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
    if free_pct() < cfg['storage']['min_free_percent']:
        touch_node(node, "low_storage")
        return jsonify({"error": "Insufficient storage"}), 507
    f = request.files.get('file')
    if not f:
        return "No file", 400
    fname = secure_filename(f.filename)
    subdir = datetime.datetime.utcnow().strftime("%Y%m%d")
    dst_dir = os.path.join(cfg['storage']['base_dir'], node or "unknown", subdir)
    os.makedirs(dst_dir, exist_ok=True)
    path = os.path.join(dst_dir, fname)
    f.save(path)
    with db_conn() as db:
        db.execute("INSERT INTO clips(node_id, filepath, timestamp) VALUES (?,?,?)",
                   (node, path, _utcnow_iso()))
        db.commit()
    touch_node(node, "online")
    return "OK", 200

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
            body { background:#000; color:#fff; font-family: Arial, Helvetica, sans-serif; margin:0; }
            header { text-align:center; padding:12px 8px; font-size:24px; font-weight:700; }
            .status { text-align:center; font-size:18px; padding:6px 0; }
            .ok { color:#00ff7f; }
            .warn { color:#ff4d4d; }
            .list { padding:10px 14px; }
            .node { font-size:18px; padding:6px 0; border-bottom:1px solid #222; }
            .badge { display:inline-block; min-width:80px; padding:2px 8px; border-radius:6px; text-align:center; margin-right:8px; }
            .online { background:#103; color:#6cf; }
            .offline { background:#301; color:#f88; }
            .muted { color:#aaa; }
            .footer { text-align:center; padding:10px; font-size:12px; color:#888; }
        </style>
    </head>
    <body>
        <header>Hub Server Status</header>
        <div class="status {{ 'warn' if pct < min_free else 'ok' }}">
            {{ '⚠ LOW STORAGE' if pct < min_free else 'Storage OK' }} — {{ "%.1f" % pct }}% free
        </div>
        <div class="list">
            {% if not nodes %}
                <div class="node muted">No nodes registered yet…</div>
            {% else %}
                {% for node, last_seen, status in nodes %}
                    <div class="node">
                        {% set is_online = (last_seen and last_seen != '') %}
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
    return render_template_string(html, pct=pct, min_free=cfg['storage']['min_free_percent'], nodes=nodes)

if __name__ == "__main__":
    # Dev mode: python3 server.py (not used under systemd)
    app.run(host="0.0.0.0", port=5000)
