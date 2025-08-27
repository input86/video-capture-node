# /home/pi/hub_server/server.py
from flask import Flask, request, jsonify, render_template_string
import os
import sqlite3
import yaml
import datetime
import shutil
import secrets
from pathlib import Path
from werkzeug.utils import secure_filename

# -------- Config --------
CFG_PATH = Path(__file__).with_name("config.yaml")
cfg = yaml.safe_load(open(CFG_PATH)) if CFG_PATH.exists() else {}

DB_PATH = cfg.get("database", "/home/pi/data/hub.db")
STORAGE = cfg.get("storage", {}) or {}
STORAGE_BASE = STORAGE.get("base_dir", "/home/pi/data")
MIN_FREE_PCT = float(STORAGE.get("min_free_percent", 10))
CLIPS_SUB = STORAGE.get("clips_subdir", "clips")
CLIPS_BASE = os.path.join(STORAGE_BASE, CLIPS_SUB)

CLAIM_KEY = cfg.get("claim_key")  # must be set in config.yaml

os.makedirs(CLIPS_BASE, exist_ok=True)
app = Flask(__name__)

# -------- DB helpers --------
def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _ensure_nodes_columns():
    with db_conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS nodes(
                node_id   TEXT PRIMARY KEY,
                last_seen TEXT,
                status    TEXT
            );
        """)
        # Add optional columns if missing
        cols = {r[1] for r in db.execute("PRAGMA table_info(nodes);")}
        for name, typ in (
            ("ip", "TEXT"),
            ("version", "TEXT"),
            ("free_space_pct", "REAL"),
            ("queue_len", "INTEGER"),
        ):
            if name not in cols:
                db.execute(f"ALTER TABLE nodes ADD COLUMN {name} {typ};")
        db.commit()

def _ensure_clips_table():
    with db_conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS clips(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id   TEXT NOT NULL,
                filepath  TEXT NOT NULL,   -- relative to STORAGE_BASE (e.g. clips/cam01/...)
                timestamp TEXT NOT NULL
            );
        """)
        db.commit()

def _ensure_tokens_table():
    with db_conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS node_tokens(
                node_id    TEXT PRIMARY KEY,
                token      TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        db.commit()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _ensure_nodes_columns()
    _ensure_clips_table()
    _ensure_tokens_table()

init_db()

# -------- Utilities --------
def _utcnow_iso():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

def free_pct():
    total, used, free = shutil.disk_usage(STORAGE_BASE)
    return (free / total) * 100.0 if total else 0.0

def _node_from_token_db(token: str):
    try:
        with db_conn() as db:
            cur = db.execute("SELECT node_id FROM node_tokens WHERE token=?;", (token,))
            r = cur.fetchone()
            return r["node_id"] if r else None
    except Exception:
        return None

def node_from_token(token):
    """Resolve node by token: DB first, then legacy config fallback."""
    if not token:
        return None
    nid = _node_from_token_db(token)
    if nid:
        return nid
    for node, t in (cfg.get("auth_tokens", {}) or {}).items():
        if t == token:
            return node
    return None

def _upsert_token(node_id: str) -> str:
    """Return existing token for node_id or create a new one."""
    tok = None
    with db_conn() as db:
        cur = db.execute("SELECT token FROM node_tokens WHERE node_id=?;", (node_id,))
        r = cur.fetchone()
        if r:
            tok = r["token"]
        else:
            tok = secrets.token_hex(24)  # 48 hex chars
            db.execute("INSERT INTO node_tokens(node_id, token) VALUES(?, ?);", (node_id, tok))
            db.commit()
    return tok

def _hub_ssh_pubkey() -> str:
    """Return the hub's SSH public key (id_ed25519 preferred), or empty string."""
    home = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        p = home / name
        if p.exists():
            try:
                return p.read_text().strip()
            except Exception:
                pass
    return ""

def touch_node(node_id: str, status: str = "online", **kwargs):
    """
    kwargs may include: ip, version, free_space_pct, queue_len.
    Only non-None are written.
    """
    fields = {"last_seen": _utcnow_iso(), "status": status}
    for k in ("ip", "version", "free_space_pct", "queue_len"):
        v = kwargs.get(k, None)
        if v is not None:
            fields[k] = v

    with db_conn() as db:
        db.execute("INSERT OR IGNORE INTO nodes(node_id) VALUES (?);", (node_id,))
        sets = ", ".join([f"{k}=?" for k in fields.keys()])
        vals = list(fields.values()) + [node_id]
        db.execute(f"UPDATE nodes SET {sets} WHERE node_id=?;", vals)
        db.commit()

def fetch_nodes():
    with db_conn() as db:
        cur = db.execute("SELECT node_id, last_seen, status, ip, version, free_space_pct, queue_len FROM nodes ORDER BY node_id;")
        return cur.fetchall()

# -------- API --------

@app.post("/api/v1/claim")
def claim_node():
    """
    First-boot claim:
      body: { "node_id":"camNN", "claim_key":"<from hub_server/config.yaml>" }
      returns: { ok, node_id, auth_token, hub_ssh_pubkey }
    Idempotent: if node_id already has a token, returns the existing one.
    """
    data = request.get_json(silent=True) or {}
    node_id = str(data.get("node_id", "")).strip()
    key = str(data.get("claim_key", "")).strip()

    if not node_id:
        return jsonify({"ok": False, "error": "node_id required"}), 400
    if not CLAIM_KEY:
        return jsonify({"ok": False, "error": "hub claim_key not configured"}), 500
    if key != CLAIM_KEY:
        return jsonify({"ok": False, "error": "invalid claim_key"}), 401

    token = _upsert_token(node_id)
    # Ensure the node appears in the nodes table immediately
    touch_node(node_id, status="claimed")
    return jsonify({
        "ok": True,
        "node_id": node_id,
        "auth_token": token,
        "hub_ssh_pubkey": _hub_ssh_pubkey()
    }), 200

@app.route("/api/v1/heartbeat", methods=["POST"])
def heartbeat_legacy():
    """
    Legacy (kept for compatibility with older nodes that POST here).
    Just validates token and updates last_seen; returns free%.
    """
    node = node_from_token(request.headers.get("X-Auth-Token"))
    if not node:
        return "Unauthorized", 401

    # Prefer X-Forwarded-For if present
    ip_hdr = request.headers.get("X-Forwarded-For", "")
    ip = (ip_hdr.split(",")[0].strip() if ip_hdr else request.remote_addr)
    touch_node(node, "online", ip=ip)

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

    # Save under /home/pi/data/<clips_subdir>/<node>/<YYYYMMDD>/<fname>
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

    # Update node presence + IP source
    ip_hdr = request.headers.get("X-Forwarded-For", "")
    ip = (ip_hdr.split(",")[0].strip() if ip_hdr else request.remote_addr)
    touch_node(node, "online", ip=ip)

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
                    <div class="node">
                        <span class="badge {{ 'online' if (status or 'offline') != 'offline' else 'offline' }}">{{ (status or 'offline').upper() }}</span>
                        <strong>{{ node }}</strong>
                        <span class="muted">— {{ status or '—' }} @ {{ last_seen or '—' }}</span>
                    </div>
                {% endfor %}
            {% endif %}
        </div>
        <div class="footer">/api/v1/claim · /api/v1/heartbeat · /api/v1/clips</div>
    </body>
    </html>
    """
    return render_template_string(html, pct=pct, min_free=float(MIN_FREE_PCT), nodes=nodes)

# -------- Dev entrypoint --------
if __name__ == "__main__":
    # For ad-hoc testing only; systemd/gunicorn runs this in production.
    app.run(host="0.0.0.0", port=5000)
