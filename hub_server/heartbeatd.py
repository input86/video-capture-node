#!/usr/bin/env python3
import sqlite3, time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from flask import Flask, request, jsonify, Response

from heartbeat_config import (
    DB_PATH, NODE_TOKENS, HOST, PORT,
    HEARTBEAT_ONLINE_SEC, HEARTBEAT_STALE_SEC
)

app = Flask(__name__)

# ---------------- DB helpers ----------------

def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def table_cols() -> List[str]:
    with db() as con:
        cur = con.execute("PRAGMA table_info(nodes);")
        return [r[1] for r in cur.fetchall()]

def pk_col() -> str:
    cols = set(table_cols())
    if "node_id" in cols: return "node_id"
    if "id" in cols: return "id"
    # Create table if it doesn't exist yet — match your current schema (node_id)
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
          node_id TEXT PRIMARY KEY,
          last_seen TEXT,
          status TEXT,
          ip TEXT,
          version TEXT,
          free_space_pct REAL,
          queue_len INTEGER
        );
        """)
        con.commit()
    return "node_id"

def ensure_columns():
    cols = set(table_cols())
    wanted = [
        ("last_seen","TEXT"),
        ("ip","TEXT"),
        ("version","TEXT"),
        ("free_space_pct","REAL"),
        ("queue_len","INTEGER"),
    ]
    with db() as con:
        for name, typ in wanted:
            if name not in cols:
                con.execute(f"ALTER TABLE nodes ADD COLUMN {name} {typ};")
        con.commit()

def ensure_schema():
    pk_col()
    ensure_columns()

ensure_schema()

# -------------- status helpers --------------

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def iso_to_ts(iso_str: Optional[str]) -> Optional[float]:
    if not iso_str: return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try: return datetime.strptime(iso_str, fmt).timestamp()
        except ValueError: pass
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

def status_from_last_seen(last_seen_iso: Optional[str], now_ts: float) -> str:
    ts = iso_to_ts(last_seen_iso)
    if ts is None: return "offline"
    delta = now_ts - ts
    if delta <= HEARTBEAT_ONLINE_SEC: return "online"
    if delta <= HEARTBEAT_STALE_SEC:  return "stale"
    return "offline"

def get_nodes() -> List[Dict[str, Any]]:
    pk = pk_col()
    with db() as con:
        cur = con.execute(f"SELECT * FROM nodes ORDER BY {pk};")
        rows = [dict(r) for r in cur.fetchall()]
    now = time.time()
    for r in rows:
        r["status"] = status_from_last_seen(r.get("last_seen"), now)
        if "node_id" not in r and "id" in r:
            r["node_id"] = r["id"]
    return rows

def upsert_node(node_id: str, fields: Dict[str, Any]):
    pk = pk_col()
    with db() as con:
        con.execute(f"INSERT OR IGNORE INTO nodes ({pk}) VALUES (?);", (node_id,))
        sets = ", ".join([f"{k}=?" for k in fields.keys()])
        vals = list(fields.values()) + [node_id]
        con.execute(f"UPDATE nodes SET {sets} WHERE {pk}=?;", vals)
        con.commit()

def auth_ok(node_id: str, token: str) -> bool:
    expected = NODE_TOKENS.get(node_id)
    return (expected is not None) and (token == expected)

# ------------------ routes ------------------

@app.post("/api/v1/heartbeat")
def heartbeat():
    token = request.headers.get("X-Auth-Token", "")
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400

    node_id = (payload or {}).get("node_id")
    if not node_id:
        return jsonify({"error": "node_id required"}), 400
    if not auth_ok(node_id, token):
        return jsonify({"error": "unauthorized"}), 401

    ip        = request.headers.get("X-Forwarded-For") or request.remote_addr
    version   = (payload or {}).get("version")
    free_pct  = (payload or {}).get("free_space_pct")
    queue_len = (payload or {}).get("queue_len")

    upsert_node(node_id, {
        "last_seen": utcnow_iso(),
        "ip": ip,
        "version": version,
        "free_space_pct": free_pct,
        "queue_len": queue_len
    })
    return jsonify({"ok": True, "server_time": utcnow_iso()})

@app.get("/api/v1/nodes")
def api_nodes():
    return jsonify({"nodes": get_nodes()})

@app.get("/")
def root():
    return jsonify({"nodes": get_nodes()})

# Tiny HTML UI (optional, for quick glance)
_UI = """<!doctype html><meta charset="utf-8"><title>Nodes</title>
<style>body{font-family:system-ui;background:#0b0f12;color:#e5e7eb;margin:10px}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:10px;margin:8px 0}
.chip{padding:4px 10px;border-radius:999px;color:#fff;font-weight:700}
.online{background:#16a34a}.stale{background:#f59e0b}.offline{background:#dc2626}</style>
<h1>Camera Nodes</h1><div id=g></div><div id=t style=color:#94a3b8></div>
<script>
async function load(){let r=await fetch('/api/v1/nodes'),j=await r.json(),g=document.getElementById('g');g.innerHTML='';
(j.nodes||[]).forEach(n=>{let e=document.createElement('div');e.className='card';
e.innerHTML=`<div><b>${n.node_id||n.id}</b> <span class="chip ${(n.status||'offline')}">${(n.status||'offline').toUpperCase()}</span></div>
<div>Last: ${n.last_seen||'—'} · IP: ${n.ip||'—'} · Ver: ${n.version||'—'} · Free%: ${n.free_space_pct??'—'} · Queue: ${n.queue_len??'—'}</div>`;
g.appendChild(e);}); document.getElementById('t').textContent='Updated '+new Date().toLocaleTimeString();}
load(); setInterval(load,3000);
</script>"""
from flask import Response
@app.get("/ui")
def ui():
    return Response(_UI, mimetype="text/html")

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
