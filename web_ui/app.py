#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
import subprocess
import re
import tempfile
import json as pyjson
import urllib.request
import urllib.error
import socket
import time
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from flask import (
    Flask, jsonify, render_template, abort, Response,
    request, make_response, send_file
)
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parent

# ---------- Load hub_server/config.yaml ----------
DEFAULT_HUB_CFG = Path.home() / "hub_server" / "config.yaml"
HUB_CFG_PATH = Path(os.environ.get("HUB_SERVER_CONFIG", str(DEFAULT_HUB_CFG)))

def load_hub_cfg() -> dict:
    if not HUB_CFG_PATH.exists():
        raise FileNotFoundError(
            f"Hub config not found at {HUB_CFG_PATH}. "
            f"Set HUB_SERVER_CONFIG if it lives elsewhere."
        )
    with open(HUB_CFG_PATH, "r") as f:
        return yaml.safe_load(f) or {}

hub_cfg = load_hub_cfg()

DB_PATH = hub_cfg.get("database")
if not DB_PATH:
    raise RuntimeError("Missing 'database' in hub_server/config.yaml")

storage = hub_cfg.get("storage", {}) or {}
base_dir = storage.get("base_dir", "/home/pi/data")
clips_subdir = storage.get("clips_subdir", "clips")
CLIPS_DIR = Path(base_dir) / clips_subdir
MIN_FREE_PCT = float(storage.get("min_free_percent", 10))

HB_ONLINE = int(os.environ.get("HB_ONLINE_SEC", "10"))
HB_STALE  = int(os.environ.get("HB_STALE_SEC", "30"))

app = Flask(__name__)



# --------- Profiles catalog (server-side source of truth for UI & validation) ---------
PROFILES: Dict[str, Dict[str, Any]] = {
    "balanced_1080p30": {
        "resolution": "1920x1080", "fps": 30, "gop": 60,
        "h264_profile": "high", "h264_level": "4.1",
        "default_bitrate_kbps": 14000, "recommended_bitrate_kbps": [12000, 18000],
        "default_rotation": 0
    },
    "action_1080p60": {
        "resolution": "1920x1080", "fps": 60, "gop": 120,
        "h264_profile": "high", "h264_level": "4.2",
        "default_bitrate_kbps": 24000, "recommended_bitrate_kbps": [22000, 28000],
        "default_rotation": 0
    },
    "storage_saver_720p30": {
        "resolution": "1280x720", "fps": 30, "gop": 60,
        "h264_profile": "high", "h264_level": "4.0",
        "default_bitrate_kbps": 7000, "recommended_bitrate_kbps": [6000, 10000],
        "default_rotation": 0
    },
    "night_low_noise_1080p30": {
        "resolution": "1920x1080", "fps": 30, "gop": 60,
        "h264_profile": "high", "h264_level": "4.1",
        "default_bitrate_kbps": 18000, "recommended_bitrate_kbps": [16000, 22000],
        "default_rotation": 0
    },
    "smooth_720p60": {
        "resolution": "1280x720", "fps": 60, "gop": 120,
        "h264_profile": "high", "h264_level": "4.1",
        "default_bitrate_kbps": 12000, "recommended_bitrate_kbps": [10000, 16000],
        "default_rotation": 0
    },
}
DEFAULT_PROFILE = "storage_saver_720p30"

def _profile_to_res_fps(profile: str) -> Tuple[str, int]:
    p = PROFILES.get(profile) or PROFILES[DEFAULT_PROFILE]
    return p["resolution"], int(p["fps"])

def _clamp_bitrate_for_profile(profile: str, br: Optional[int]) -> int:
    p = PROFILES.get(profile) or PROFILES[DEFAULT_PROFILE]
    default_br = int(p["default_bitrate_kbps"])
    lo, hi = [int(x) for x in p["recommended_bitrate_kbps"]]
    if br is None:
        return default_br
    try:
        br = int(br)
    except Exception:
        return default_br
    return max(lo, min(hi, br))

# Pass auth badge (if any) from Nginx basic auth
@app.context_processor
def inject_admin_user():
    user = request.headers.get("X-Remote-User", "")
    return {"admin_user": user}

# -------------------- DB helpers --------------------

def db_conn():
    return sqlite3.connect(DB_PATH)

def table_exists(name: str) -> bool:
    try:
        with db_conn() as db:
            cur = db.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,))
            return cur.fetchone() is not None
    except Exception:
        return False

def get_columns(table: str):
    with db_conn() as db:
        cur = db.cursor()
        return cur.execute(f"PRAGMA table_info({table});").fetchall()

def ensure_column(table: str, column: str, coltype: str) -> None:
    try:
        cols = [r[1] for r in get_columns(table)]
        if column not in cols:
            with db_conn() as db:
                cur = db.cursor()
                cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {coltype};')
                db.commit()
    except Exception:
        pass

def init_db():
    with db_conn() as db:
        cur = db.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS camera_settings (
            camera_id TEXT PRIMARY KEY,
            resolution TEXT,
            fps INTEGER,
            bitrate_kbps INTEGER,
            rotation INTEGER,
            clip_duration_s INTEGER,
            updated_at REAL,
            profile TEXT,
            sensor_threshold_mm INTEGER
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS camera_endpoints (
            camera_id TEXT PRIMARY KEY,
            ssh_host TEXT,
            ssh_user TEXT,
            config_path TEXT,
            service_name TEXT,
            updated_at REAL
        );""")
        db.commit()
    # backfill columns for older DBs
    ensure_column("camera_settings", "profile", "TEXT")
    ensure_column("camera_settings", "sensor_threshold_mm", "INTEGER")

# -------------------- Misc helpers --------------------

def parse_any_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    # numeric epoch?
    try:
        return float(value)
    except Exception:
        pass

    s = str(value).strip()

    # If it ends with 'Z' (UTC), parse as UTC explicitly
    if s.endswith("Z"):
        try:
            # handle both with/without fractional secs
            dt = datetime.fromisoformat(s[:-1])  # naive
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass

    # Generic ISO parse. If naive (no tzinfo), **assume UTC** to match node storage & TFT.
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception:
        return None

def hb_candidate_columns(cols: List[str]) -> Optional[str]:
    candidates = [
        "last_heartbeat", "last_seen", "updated_at",
        "heartbeat_ts", "hb_ts", "last_seen_ts", "ts", "timestamp"
    ]
    lower = {c.lower(): c for c in cols}
    for name in candidates:
        if name in lower:
            return lower[name]
    for c in cols:
        lc = c.lower()
        if any(k in lc for k in ["heart","seen","time","update","stamp"]):
            return c
    return None

def node_id_candidate_columns(cols: List[str]) -> Optional[str]:
    for name in ["node_id","id","node","name"]:
        for c in cols:
            if c.lower() == name:
                return c
    return cols[0] if cols else None

def build_node_row(node_id: str, hb_ts: Optional[float], now_ts: float) -> Dict[str, Any]:
    """
    Computes unified status with correct handling of FUTURE heartbeats:
    - If heartbeat is in the future, we mark as STALE (within HB_STALE) or OFFLINE (beyond).
      We DO NOT show ONLINE for any future timestamp.
    """
    if hb_ts is None:
        return {"node_id": node_id, "last_heartbeat": None, "seconds_ago": None,
                "status": "offline", "skew_ahead": 0}

    if hb_ts > now_ts:
        skew = int(hb_ts - now_ts + 0.5)
        status = "stale" if skew <= HB_STALE else "offline"
        return {"node_id": node_id, "last_heartbeat": hb_ts,
                "seconds_ago": 0, "status": status, "skew_ahead": skew}

    delta = now_ts - hb_ts
    status = "online" if delta <= HB_ONLINE else ("stale" if delta <= HB_STALE else "offline")
    return {"node_id": node_id, "last_heartbeat": hb_ts,
            "seconds_ago": int(delta), "status": status, "skew_ahead": 0}

def get_nodes() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).timestamp()
    rows: List[Dict[str, Any]] = []
    try:
        with db_conn() as db:
            cur = db.cursor()
            if table_exists("nodes"):
                info = get_columns("nodes"); colnames = [r[1] for r in info]
                nid_col = node_id_candidate_columns(colnames)
                ts_col  = hb_candidate_columns(colnames)
                if nid_col and ts_col:
                    cur.execute(f'SELECT "{nid_col}", "{ts_col}" FROM nodes;')
                    for nid, hb in cur.fetchall():
                        ts = parse_any_ts(hb)
                        rows.append(build_node_row(str(nid), ts, now))
                    return sorted(rows, key=lambda r: (r["seconds_ago"] if r["seconds_ago"] is not None else 9e9))
                if nid_col:
                    cur.execute(f'SELECT "{nid_col}" FROM nodes;')
                    for (nid,) in cur.fetchall():
                        rows.append(build_node_row(str(nid), None, now))
                    return rows
            if table_exists("heartbeats"):
                info = get_columns("heartbeats"); colnames = [r[1] for r in info]
                nid_col = node_id_candidate_columns(colnames)
                ts_col  = hb_candidate_columns(colnames)
                if nid_col and ts_col:
                    cur.execute(f'SELECT "{nid_col}", MAX("{ts_col}") FROM heartbeats GROUP BY "{nid_col}";')
                    for nid, hb in cur.fetchall():
                        ts = parse_any_ts(hb)
                        rows.append(build_node_row(str(nid), ts, now))
                    return sorted(rows, key=lambda r: (r["seconds_ago"] if r["seconds_ago"] is not None else 9e9))
            return []
    except Exception as e:
        print("get_nodes error:", e)
        return []

def disk_free(path: Path) -> Dict[str, Any]:
    """Robust disk stats using nearest existing directory."""
    try:
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        if not target.exists():
            target = Path(base_dir) if Path(base_dir).exists() else Path("/")
        if target.is_file():
            target = target.parent
        usage = shutil.disk_usage(str(target))
        total = float(usage.total); used = float(usage.used); avail = float(usage.free)
        pct_free = (avail / total) * 100.0 if total else 0.0
        return {"total": total, "used": used, "avail": avail, "pct_free": pct_free}
    except Exception as e:
        print(f"disk_free error at {path}: {e}")
        return {"total": 0.0, "used": 0.0, "avail": 0.0, "pct_free": 0.0}

def list_recent_clips(limit: int = 200) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        if not CLIPS_DIR.exists():
            return items
        for p in sorted(CLIPS_DIR.glob("**/*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            st = p.stat()
            items.append({"rel": str(p.relative_to(CLIPS_DIR)), "size": st.st_size, "mtime": st.st_mtime})
    except Exception as e:
        print("list_recent_clips error:", e)
    return items

# >>>>>>>>>>>>>>> filtered clips helper (P1C) <<<<<<<<<<<<<<<
def list_clips_filtered(
    start_ts: Optional[float],
    end_ts: Optional[float],
    *,
    all_time: bool = False,
    sort: str = "newest",
    limit: Optional[int] = 200,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        if not CLIPS_DIR.exists():
            return items
        paths = list(CLIPS_DIR.glob("**/*.mp4"))
        reverse = (sort != "oldest")
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=reverse)
        for p in paths:
            st = p.stat()
            mt = st.st_mtime
            if not all_time:
                if (start_ts is not None and mt < start_ts) or (end_ts is not None and mt > end_ts):
                    continue
            items.append({"rel": str(p.relative_to(CLIPS_DIR)), "size": st.st_size, "mtime": mt})
            if limit is not None and len(items) >= limit:
                break
    except Exception as e:
        print("list_clips_filtered error:", e)
    return items
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# -------------------- Redacted config helpers --------------------

REDACT_KEYS = ("token","secret","password","passwd","key")

def _redact(val: Any) -> Any:
    if isinstance(val, str):
        if len(val) <= 4: return "****"
        return val[:2] + "…" + val[-2:]
    return "***"

def redact_config(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k,v in obj.items():
            if any(x in str(k).lower() for x in REDACT_KEYS):
                out[k] = _redact(v)
            else:
                out[k] = redact_config(v)
        return out
    if isinstance(obj, list):
        return [redact_config(x) for x in obj]
    return obj

def validate_cfg(cfg: dict) -> List[Dict[str,str]]:
    issues: List[Dict[str,str]] = []
    db = cfg.get("database")
    if not db:
        issues.append({"level":"error","msg":"Missing 'database' path"})
    else:
        p = Path(db)
        if not p.exists():
            issues.append({"level":"warn","msg":f"DB path does not exist: {db}"})
        elif not p.is_file():
            issues.append({"level":"warn","msg":f"DB path is not a file: {db}"})
    st = cfg.get("storage", {}) or {}
    base = Path(st.get("base_dir","/home/pi/data"))
    clips_sub = st.get("clips_subdir","clips")
    clips = base / clips_sub
    if not base.exists():
        issues.append({"level":"warn","msg":f"Base dir missing: {base}"})
    if not clips.exists():
        issues.append({"level":"warn","msg":f"Clips dir missing: {clips}"})
    mfp = st.get("min_free_percent", 10)
    try:
        mfp_f = float(mfp)
        if not (0 <= mfp_f <= 100):
            issues.append({"level":"error","msg":f"min_free_percent out of range (0-100): {mfp}"})
    except Exception:
        issues.append({"level":"error","msg":"min_free_percent not a number"})
    if "auth_tokens" not in cfg:
        issues.append({"level":"warn","msg":"No 'auth_tokens' mapping found"})
    return issues

def read_config_text(raw: bool) -> str:
    with open(HUB_CFG_PATH, "r") as f:
        cfg = yaml.safe_load(f) or {}
    if raw:
        return yaml.safe_dump(cfg, sort_keys=False)
    red = redact_config(cfg)
    return yaml.safe_dump(red, sort_keys=False)

# -------------------- SSH / HTTP helpers --------------------

def _ssh(cmd: List[str], timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _ssh_cat(ssh_user: str, ssh_host: str, path: str) -> Tuple[bool, str]:
    p = _ssh(["ssh", f"{ssh_user}@{ssh_host}", "cat", path])
    return (p.returncode == 0, p.stdout if p.returncode == 0 else p.stderr)

def _ssh_write_and_restart(ssh_user: str, ssh_host: str, path: str, content: str, service: str) -> Tuple[bool, str]:
    tmp_remote = f"{path}.tmp.{int(datetime.now().timestamp())}"
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write(content); tf.flush()
        local_tmp = tf.name
    try:
        p1 = _ssh(["scp", local_tmp, f"{ssh_user}@{ssh_host}:{tmp_remote}"])
        if p1.returncode != 0:
            return False, p1.stderr
        mv_cmd = f"mv {tmp_remote} {path}"
        sysd_cmd = f"sudo systemctl restart {service}"
        p2 = _ssh(["ssh", f"{ssh_user}@{ssh_host}", mv_cmd])
        if p2.returncode != 0:
            return False, p2.stderr
        p3 = _ssh(["ssh", f"{ssh_user}@{ssh_host}", sysd_cmd])
        if p3.returncode != 0:
            return False, p3.stderr
        return True, "ok"
    finally:
        try: os.unlink(local_tmp)
        except Exception: pass

def _http_json(url: str, method: str = "GET", body: Optional[dict] = None, timeout: int = 8) -> Tuple[bool, Any, str]:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = pyjson.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            try:
                return True, pyjson.loads(text), ""
            except Exception:
                return True, text, ""
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8")
            return False, None, err or str(e)
        except Exception:
            return False, None, str(e)
    except Exception as e:
        return False, None, str(e)

def wait_unit_state(ssh_user: str, ssh_host: str, unit: str, desired: str, timeout_sec: float = 12.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        p = _ssh(["ssh", f"{ssh_user}@{ssh_host}", f"systemctl is-active {unit}"])
        st = (p.stdout or p.stderr).strip()
        if desired == "active" and st == "active":
            return True
        if desired == "inactive" and st in ("inactive","failed","unknown"):
            return True
        time.sleep(0.35)
    return False

def wait_until_recorder_ready(ssh_user: str, ssh_host: str, unit: str, timeout_sec: float = 20.0) -> Tuple[bool, str]:
    if not wait_unit_state(ssh_user, ssh_host, unit, "active", timeout_sec):
        tail = _ssh(["ssh", f"{ssh_user}@{ssh_host}", f"journalctl -u {unit} -n 80 --no-pager --output=short"]).stdout
        return False, f"{unit} not active\n{(tail or '').strip()}"
    tail = _ssh(["ssh", f"{ssh_user}@{ssh_host}", f"journalctl -u {unit} -n 120 --no-pager --output=short"]).stdout
    if re.search(r"\[READY\]\s+Camera node started", tail or ""):
        return True, ""
    return True, (tail or "")

# -------------------- Template filters --------------------

@app.template_filter("human_bytes")
def human_bytes(n: float) -> str:
    try: n = float(n)
    except Exception: return "0 B"
    units = ["B","KB","MB","GB","TB","PB"]
    i = 0
    while n >= 1024 and i < len(units)-1:
        n /= 1024.0; i += 1
    return f"{int(n)} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"

@app.template_filter("date_fmt")
def date_fmt(ts: Optional[float]) -> str:
    if ts is None: return "—"
    return datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")

@app.template_filter("hb_fmt")
def hb_fmt(ts: Optional[float]) -> str:
    if ts is None: return "—"
    return datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")

# -------------------- Pages --------------------

@app.route("/")
def dashboard():
    nodes = get_nodes()
    counts = {
        "online": sum(1 for n in nodes if n["status"] == "online"),
        "stale":  sum(1 for n in nodes if n["status"] == "stale"),
        "offline":sum(1 for n in nodes if n["status"] == "offline"),
    }
    fs = disk_free(CLIPS_DIR)
    clips = list_recent_clips(limit=25)
    return render_template("index.html", nodes=nodes, counts=counts, fs=fs,
                           min_free_percent=MIN_FREE_PCT, clips=clips,
                           clip_base=str(CLIPS_DIR), title="Dashboard")

@app.route("/nodes")
def nodes_page():
    nodes = get_nodes()
    return render_template("nodes.html", nodes=nodes,
                           hb_online=HB_ONLINE, hb_stale=HB_STALE,
                           title="Nodes")

@app.route("/nodes.csv")
def nodes_csv():
    nodes = get_nodes()
    lines = ["node_id,last_heartbeat_utc,seconds_ago,status,skew_ahead"]
    for n in nodes:
        ts = "" if n["last_heartbeat"] is None else datetime.fromtimestamp(n["last_heartbeat"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f'{n["node_id"]},{ts},{n["seconds_ago"] if n["seconds_ago"] is not None else ""},{n["status"]},{n.get("skew_ahead",0)}')
    csv = "\n".join(lines) + "\n"
    resp = make_response(csv)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = 'attachment; filename="nodes.csv"'
    return resp

# -------------------- NEW: Unified status API --------------------

@app.route("/api/nodes")
def api_nodes():
    nodes = get_nodes()
    for n in nodes:
        ts = n.get("last_heartbeat")
        n["last_heartbeat_iso"] = None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return jsonify({
        "ok": True,
        "now_ts": datetime.now(timezone.utc).timestamp(),
        "hb_online_sec": HB_ONLINE,
        "hb_stale_sec": HB_STALE,
        "nodes": nodes
    }), 200

# -------------------- Clips --------------------

@app.route("/clips")
def clips_page():
    qs = request.args
    all_time = str(qs.get("all", "")).lower() in ("1", "true", "yes")
    sort = qs.get("sort", "newest")
    start_param = qs.get("start")
    end_param = qs.get("end")

    start_ts = parse_any_ts(start_param) if start_param else None
    end_ts = parse_any_ts(end_param) if end_param else None

    if not all_time:
        if start_ts is None or end_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()
            end_ts = now_ts
            start_ts = now_ts - 60 * 60

    limit = None if all_time else 200
    clips = list_clips_filtered(start_ts, end_ts, all_time=all_time, sort=sort, limit=limit)

    return render_template(
        "clips.html",
        clips=clips,
        clip_base=str(CLIPS_DIR),
        title="Clips",
        filter_all=all_time,
        filter_start=start_ts,
        filter_end=end_ts,
        sort=sort,
        result_count=len(clips),
    )

@app.route("/download/<path:relpath>")
def download_clip(relpath: str):
    base = CLIPS_DIR.resolve()
    file_path = (CLIPS_DIR / relpath).resolve()
    try: file_path.relative_to(base)
    except Exception: abort(404)
    if not file_path.exists() or not file_path.is_file():
        abort(404)
    internal_uri = "/__protected__/clips/" + str(file_path.relative_to(base)).replace("\\", "/")
    headers = {"Content-Disposition": f'attachment; filename="{file_path.name}"',
               "X-Accel-Redirect": internal_uri}
    return Response("", headers=headers)

@app.route("/thumb/<path:relpath>")
def thumb(relpath: str):
    base = CLIPS_DIR.resolve()
    mp4 = (CLIPS_DIR / relpath).with_suffix(".mp4").resolve()
    jpg = mp4.with_suffix(".jpg")
    try:
        mp4.relative_to(base); jpg.relative_to(base)
    except Exception:
        abort(404)
    if jpg.exists():
        internal_uri = "/__protected__/thumbs/" + str(jpg.relative_to(base)).replace("\\", "/")
        return Response("", headers={"X-Accel-Redirect": internal_uri})
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="170">
      <rect width="100%" height="100%" fill="#e5e7eb"/>
      <text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" fill="#6b7280" font-size="14">No thumbnail</text>
    </svg>'''
    return Response(svg, headers={"Content-Type": "image/svg+xml"})

# -------------------- Actions (thumbs/delete) --------------------

def prune_db_rows_for_clip(mp4_path: Path) -> int:
    try:
        if not table_exists("clips"):
            return 0
        rel = str(mp4_path.relative_to(CLIPS_DIR).as_posix())
        abs_path = str(mp4_path)
        base = mp4_path.name
        with db_conn() as db:
            cur = db.cursor()
            cols = [r[1] for r in get_columns("clips")]
            pref_order = ["relpath","relative_path","path","filepath","file_path","clip_path","clip","filename","name"]
            cand = None; lower = {c.lower(): c for c in cols}
            for k in pref_order:
                if k in lower: cand = lower[k]; break
            if cand is None:
                for c in cols:
                    lc = c.lower()
                    if any(x in lc for x in ["path","file","name","clip"]):
                        cand = c; break
            if cand is None:
                return 0
            deleted = 0
            for q, arg in [
                (f'DELETE FROM clips WHERE "{cand}" = ?;', rel),
                (f'DELETE FROM clips WHERE "{cand}" = ?;', abs_path),
                (f'DELETE FROM clips WHERE "{cand}" LIKE ?;', f'%/{base}'),
                (f'DELETE FROM clips WHERE "{cand}" = ?;', base),
            ]:
                cur.execute(q, (arg,))
                deleted += cur.rowcount if cur.rowcount is not None else 0
            db.commit()
            return deleted
    except Exception as e:
        print("prune_db_rows_for_clip error:", e)
        return 0

@app.route("/action/thumbs/run", methods=["POST"])
def run_thumbs():
    py = str((APP_DIR / "venv" / "bin" / "python"))
    script = str(APP_DIR / "thumbs.py")
    if not Path(py).exists(): py = "python3"
    if not Path(script).exists():
        return jsonify({"ok": False, "error": "thumbs.py not found"}), 500
    try:
        proc = subprocess.run([py, script, "--prune"], cwd=str(APP_DIR),
                              capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "thumbnail job timed out"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"built=(\d+)\s+removed=(\d+)", out)
    built = int(m.group(1)) if m else 0
    removed = int(m.group(2)) if m else 0
    ok = proc.returncode == 0
    return jsonify({"ok": ok, "built": built, "removed": removed, "raw": out[-4000:]}), (200 if ok else 500)

@app.route("/action/clip/delete", methods=["POST"])
def delete_clip():
    data = request.get_json(silent=True) or {}
    rel = data.get("relpath", "")
    if not rel or "/" not in rel:
        return jsonify({"ok": False, "error": "invalid relpath"}), 400
    if not rel.lower().endswith(".mp4"):
        return jsonify({"ok": False, "error": "only .mp4 deletions allowed"}), 400
    base = CLIPS_DIR.resolve()
    mp4 = (CLIPS_DIR / rel).resolve()
    try: mp4.relative_to(base)
    except Exception: return jsonify({"ok": False, "error": "path outside clip dir"}), 400
    if not mp4.exists() or not mp4.is_file():
        pruned = prune_db_rows_for_clip(mp4)
        return jsonify({"ok": True, "deleted": {"mp4": False, "jpg": False}, "db_pruned": pruned}), 200
    jpg = mp4.with_suffix(".jpg")
    deleted = {"mp4": False, "jpg": False}
    try:
        mp4.unlink(); deleted["mp4"] = True
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to delete video: {e}"}), 500
    try:
        if jpg.exists(): jpg.unlink(); deleted["jpg"] = True
    except Exception as e:
        pruned = prune_db_rows_for_clip(mp4)
        return jsonify({"ok": False, "error": f"video deleted, but failed to delete thumbnail: {e}",
                        "deleted": deleted, "db_pruned": pruned}), 500
    try:
        p = mp4.parent
        while p != base:
            if not any(p.iterdir()):
                p.rmdir(); p = p.parent
            else:
                break
    except Exception: pass
    pruned = prune_db_rows_for_clip(mp4)
    return jsonify({"ok": True, "deleted": deleted, "db_pruned": pruned}), 200

# -------------------- CONFIG CENTER --------------------

@app.route("/config/")
def config_home():
    try:
        with open(HUB_CFG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    red = redact_config(cfg)
    text_red = yaml.safe_dump(red, sort_keys=False)
    issues = validate_cfg(cfg)
    return render_template("config.html", cfg_text=text_red,
                           cfg_path=str(HUB_CFG_PATH), issues=issues, title="Config")

@app.route("/config/reload", methods=["POST"])
def config_reload():
    try:
        with open(HUB_CFG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        red = redact_config(cfg)
        text_red = yaml.safe_dump(red, sort_keys=False)
        issues = validate_cfg(cfg)
        return jsonify({"ok": True, "text": text_red, "issues": issues}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/config/download")
def config_download_raw():
    return send_file(str(HUB_CFG_PATH), mimetype="text/yaml",
                     as_attachment=True, download_name="config.yaml")

@app.route("/config/download/redacted")
def config_download_redacted():
    text = read_config_text(raw=False)
    resp = make_response(text)
    resp.headers["Content-Type"] = "text/yaml"
    resp.headers["Content-Disposition"] = 'attachment; filename="config.redacted.yaml"'
    return resp

# -------------------- CAMERA SETTINGS (profile-based) --------------------

def list_camera_ids() -> List[str]:
    toks = hub_cfg.get("auth_tokens", {}) or {}
    ids = sorted(toks.keys())
    return [i for i in ids if i.lower().startswith("cam") or i.lower().startswith("node")]

def get_camera_settings(camera_id: str) -> Dict[str, Any]:
    ensure_column("camera_settings", "profile", "TEXT")
    ensure_column("camera_settings", "sensor_threshold_mm", "INTEGER")
    with db_conn() as db:
        cur = db.cursor()
        cur.execute("""
            SELECT camera_id, resolution, fps, bitrate_kbps, rotation, clip_duration_s, updated_at,
                   profile, sensor_threshold_mm
            FROM camera_settings WHERE camera_id = ?;
        """, (camera_id,))
        row = cur.fetchone()
        if not row:
            res, fps = _profile_to_res_fps(DEFAULT_PROFILE)
            return {
                "camera_id": camera_id,
                "profile": DEFAULT_PROFILE,
                "resolution": res,
                "fps": fps,
                "bitrate_kbps": PROFILES[DEFAULT_PROFILE]["default_bitrate_kbps"],
                "rotation": 0,
                "clip_duration_s": 5,
                "sensor_threshold_mm": 1000,
                "updated_at": None
            }
        profile = row[7] if len(row) > 7 and row[7] else DEFAULT_PROFILE
        res = row[1] or _profile_to_res_fps(profile)[0]
        fps = int(row[2]) if row[2] is not None else _profile_to_res_fps(profile)[1]
        sensor_thr = int(row[8]) if len(row) > 8 and row[8] is not None else 1000
        return {
            "camera_id": row[0],
            "profile": profile,
            "resolution": res,
            "fps": int(fps),
            "bitrate_kbps": int(row[3]) if row[3] is not None else PROFILES[profile]["default_bitrate_kbps"],
            "rotation": int(row[4]) if row[4] is not None else 0,
            "clip_duration_s": int(row[5]) if row[5] is not None else 5,
            "sensor_threshold_mm": sensor_thr,
            "updated_at": row[6],
        }

def upsert_camera_settings(payload: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).timestamp()
    cam_id = payload["camera_id"]
    profile = payload.get("profile") or DEFAULT_PROFILE
    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    br = _clamp_bitrate_for_profile(profile, payload.get("bitrate_kbps"))
    try:
        rot = int(payload.get("rotation", 0))
    except Exception:
        rot = 0
    if rot not in (0,90,180,270):
        rot = 0
    try:
        dur = int(payload.get("clip_duration_s", 5))
    except Exception:
        dur = 5
    dur = max(2, min(600, dur))

    try:
        thr = payload.get("sensor_threshold_mm", 1000)
        thr = int(thr if thr is not None else 1000)
    except Exception:
        thr = 1000
    thr = max(30, min(4000, thr))

    res, fps = _profile_to_res_fps(profile)

    with db_conn() as db:
        cur = db.cursor()
        cur.execute("""
            INSERT INTO camera_settings (camera_id, resolution, fps, bitrate_kbps, rotation, clip_duration_s, updated_at, profile, sensor_threshold_mm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(camera_id) DO UPDATE SET
              resolution=excluded.resolution,
              fps=excluded.fps,
              bitrate_kbps=excluded.bitrate_kbps,
              rotation=excluded.rotation,
              clip_duration_s=excluded.clip_duration_s,
              updated_at=excluded.updated_at,
              profile=excluded.profile,
              sensor_threshold_mm=excluded.sensor_threshold_mm;
        """, (
            cam_id, res, int(fps), int(br), int(rot), int(dur), now, profile, int(thr)
        ))
        db.commit()

def get_camera_endpoint(camera_id: str) -> Dict[str, Any]:
    with db_conn() as db:
        cur = db.cursor()
        cur.execute("""
            SELECT camera_id, ssh_host, ssh_user, config_path, service_name, updated_at
            FROM camera_endpoints WHERE camera_id = ?;
        """, (camera_id,))
        row = cur.fetchone()
        if not row:
            return {
                "camera_id": camera_id,
                "ssh_host": "",
                "ssh_user": "pi",
                "config_path": "/home/pi/camera_node/config.yaml",
                "service_name": "camera-node",
                "updated_at": None
            }
        return {
            "camera_id": row[0],
            "ssh_host": row[1] or "",
            "ssh_user": row[2] or "pi",
            "config_path": row[3] or "/home/pi/camera_node/config.yaml",
            "service_name": row[4] or "camera-node",
            "updated_at": row[5],
        }

def upsert_camera_endpoint(payload: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).timestamp()
    with db_conn() as db:
        cur = db.cursor()
        cur.execute("""
            INSERT INTO camera_endpoints (camera_id, ssh_host, ssh_user, config_path, service_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(camera_id) DO UPDATE SET
              ssh_host=excluded.ssh_host,
              ssh_user=excluded.ssh_user,
              config_path=excluded.config_path,
              service_name=excluded.service_name,
              updated_at=excluded.updated_at;
        """, (
            payload["camera_id"],
            payload.get("ssh_host",""),
            payload.get("ssh_user","pi"),
            payload.get("config_path","/home/pi/camera_node/config.yaml"),
            payload.get("service_name","camera-node"),
            now
        ))
        db.commit()

def _infer_profile_from_legacy(resolution: str, fps: int) -> str:
    key = None
    res = (resolution or "").lower().strip()
    try: fps_i = int(fps)
    except Exception: fps_i = 30
    for name, p in PROFILES.items():
        if p["resolution"].lower() == res and int(p["fps"]) == fps_i:
            key = name
            break
    return key or DEFAULT_PROFILE

def read_node_recording_yaml(ep: Dict[str,str]) -> Tuple[Optional[Dict[str,Any]], Optional[str]]:
    host = ep.get("ssh_host") or ""
    user = ep.get("ssh_user") or "pi"
    path = ep.get("config_path") or "/home/pi/camera_node/config.yaml"
    if not host:
        return None, "ssh_host not set"
    ok, text = _ssh_cat(user, host, path)
    if not ok:
        return None, text.strip() or "ssh read failed"
    try:
        cfg = yaml.safe_load(text) or {}
    except Exception as e:
        return None, f"parse failed: {e}"

    profile = cfg.get("profile")
    bitrate = cfg.get("bitrate_kbps")
    rotation = cfg.get("rotation")
    rec = cfg.get("recording") or {}
    duration = rec.get("duration_s")

    sensor_cfg = cfg.get("sensor") or {}
    sensor_threshold_mm = sensor_cfg.get("threshold_mm", None)

    if not profile:
        res_legacy = str(rec.get("resolution","1920x1080"))
        fps_legacy = int(rec.get("framerate", rec.get("fps", 15)))
        profile = _infer_profile_from_legacy(res_legacy, fps_legacy)
        if bitrate is None:
            bitrate = rec.get("bitrate_kbps")
        if rotation is None:
            rotation = rec.get("rotation", 0)
        if duration is None:
            duration = rec.get("duration_s", 5)

    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    bitrate = _clamp_bitrate_for_profile(profile, None if bitrate is None else int(bitrate))
    try:
        rotation = int(rotation if rotation is not None else 0)
    except Exception:
        rotation = 0
    if rotation not in (0,90,180,270):
        rotation = 0
    try:
        duration = int(duration if duration is not None else 5)
    except Exception:
        duration = 5
    duration = max(2, min(600, duration))

    try:
        if sensor_threshold_mm is not None:
            sensor_threshold_mm = int(sensor_threshold_mm)
    except Exception:
        sensor_threshold_mm = None
    if sensor_threshold_mm is not None:
        sensor_threshold_mm = max(30, min(4000, int(sensor_threshold_mm)))

    res, fps = _profile_to_res_fps(profile)

    out: Dict[str, Any] = {
        "profile": profile,
        "bitrate_kbps": bitrate,
        "rotation": rotation,
        "clip_duration_s": duration,
        "resolution": res,
        "fps": fps,
    }
    if sensor_threshold_mm is not None:
        out["sensor_threshold_mm"] = sensor_threshold_mm

    return out, None

@app.route("/config/cameras")
def config_cameras_page():
    cams = list_camera_ids()
    camera_rows = []
    endpoints = {c: get_camera_endpoint(c) for c in cams}
    for cam in cams:
        ep = endpoints[cam]
        node_vals, err = read_node_recording_yaml(ep)
        if node_vals:
            row = get_camera_settings(cam)
            row.update(node_vals)
            row["source"] = "node"
            camera_rows.append(row)
        else:
            row = get_camera_settings(cam)
            row["source"] = "hub"
            row["node_error"] = err
            camera_rows.append(row)
    return render_template("config_cameras.html",
                           cameras=camera_rows, endpoints=endpoints,
                           profiles=PROFILES,
                           title="Camera Settings")

@app.route("/action/secure/cameras/save", methods=["POST"])
def cameras_save():
    data = request.get_json(silent=True) or {}
    cam_id = (data.get("camera_id") or "").strip()
    if not cam_id:
        return jsonify({"ok": False, "error": "camera_id required"}), 400

    profile = (data.get("profile") or DEFAULT_PROFILE).strip()
    if profile not in PROFILES:
        return jsonify({"ok": False, "error": "invalid profile"}), 400

    current = get_camera_settings(cam_id)

    try:
        br_in = data.get("bitrate_kbps", None)
        br = _clamp_bitrate_for_profile(profile, None if br_in is None else int(br_in))
        rot = int(data.get("rotation", current.get("rotation", 0)))
        dur = int(data.get("clip_duration_s", current.get("clip_duration_s", 5)))
    except Exception:
        return jsonify({"ok": False, "error": "invalid numeric field(s)"}), 400
    if rot not in (0,90,180,270):
        return jsonify({"ok": False, "error": "rotation must be one of 0,90,180,270"}), 400
    if not (2 <= dur <= 600):
        return jsonify({"ok": False, "error": "clip_duration_s out of range (2-600)"}), 400

    try:
        thr_in = data.get("sensor_threshold_mm", current.get("sensor_threshold_mm", 1000))
        thr = int(thr_in)
    except Exception:
        return jsonify({"ok": False, "error": "sensor_threshold_mm must be an integer"}), 400
    if not (30 <= thr <= 4000):
        return jsonify({"ok": False, "error": "sensor_threshold_mm out of range (30-4000)"}), 400

    upsert_camera_settings({
        "camera_id": cam_id,
        "profile": profile,
        "bitrate_kbps": br,
        "rotation": rot,
        "clip_duration_s": dur,
        "sensor_threshold_mm": thr,
    })
    return jsonify({"ok": True}), 200

@app.route("/action/secure/cameras/save_endpoint", methods=["POST"])
def cameras_save_endpoint():
    data = request.get_json(silent=True) or {}
    cam_id = (data.get("camera_id") or "").strip()
    if not cam_id: return jsonify({"ok": False, "error": "camera_id required"}), 400
    upsert_camera_endpoint({
        "camera_id": cam_id,
        "ssh_host": (data.get("ssh_host") or "").strip(),
        "ssh_user": (data.get("ssh_user") or "pi").strip(),
        "config_path": (data.get("config_path") or "/home/pi/camera_node/config.yaml").strip(),
        "service_name": (data.get("service_name") or "camera-node").strip(),
    })
    return jsonify({"ok": True}), 200

@app.route("/action/secure/cameras/push", methods=["POST"])
def cameras_push_to_node():
    data = request.get_json(silent=True) or {}
    cam_id = (data.get("camera_id") or "").strip()
    if not cam_id: return jsonify({"ok": False, "error": "camera_id required"}), 400
    cs = get_camera_settings(cam_id)
    ep = get_camera_endpoint(cam_id)
    host,user,cfg_path,svc = ep["ssh_host"], ep["ssh_user"], ep["config_path"], ep["service_name"]
    if not host: return jsonify({"ok": False, "error": "ssh_host not set for this camera"}), 400
    ok, text = _ssh_cat(user, host, cfg_path)
    if not ok: return jsonify({"ok": False, "error": f"ssh read failed: {text.strip()}"}), 500
    try:
        cfg = yaml.safe_load(text) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"parse remote YAML failed: {e}"}), 500

    profile = cs.get("profile") or DEFAULT_PROFILE
    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    cfg["profile"] = profile
    cfg["bitrate_kbps"] = int(cs.get("bitrate_kbps", PROFILES[profile]["default_bitrate_kbps"]))
    cfg["rotation"] = int(cs.get("rotation", 0))

    rec = cfg.get("recording") or {}
    rec["duration_s"] = int(cs.get("clip_duration_s", 5))
    res, fps = _profile_to_res_fps(profile)
    rec["resolution"] = res
    rec["framerate"] = int(fps)
    cfg["recording"] = rec

    sensor_cfg = cfg.get("sensor") or {}
    sensor_cfg["threshold_mm"] = int(cs.get("sensor_threshold_mm", 1000))
    cfg["sensor"] = sensor_cfg

    new_text = yaml.safe_dump(cfg, sort_keys=False)
    ok, msg = _ssh_write_and_restart(user, host, cfg_path, new_text, svc)
    if not ok: return jsonify({"ok": False, "error": f"ssh write/restart failed: {msg.strip()}"}), 500
    return jsonify({"ok": True}), 200

@app.route("/action/secure/cameras/import_from_node", methods=["POST"])
def cameras_import_from_node():
    data = request.get_json(silent=True) or {}
    cam_id = (data.get("camera_id") or "").strip()
    if not cam_id: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam_id)
    node_vals, err = read_node_recording_yaml(ep)
    if not node_vals:
        return jsonify({"ok": False, "error": f"read node failed: {err or 'unknown'}"}), 500
    node_vals["camera_id"] = cam_id
    if "sensor_threshold_mm" not in node_vals:
        node_vals["sensor_threshold_mm"] = get_camera_settings(cam_id).get("sensor_threshold_mm", 1000)
    upsert_camera_settings(node_vals)
    return jsonify({"ok": True, "settings": node_vals}), 200

# -------------------- PREVIEW (node in-process LIVE) --------------------

@app.route("/preview/<camera_id>")
def preview_page(camera_id: str):
    ep = get_camera_endpoint(camera_id)
    host = ep.get("ssh_host") or ""
    return render_template("preview.html",
                           camera_id=camera_id,
                           endpoint=ep,
                           preview_host=host,
                           title="Preview")

@app.route("/action/secure/preview/start", methods=["POST"])
def preview_start():
    data = request.get_json(silent=True) or {}
    cam = (data.get("camera_id") or "").strip()
    if not cam: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam)
    host = ep.get("ssh_host") or ""
    if not host: return jsonify({"ok": False, "error": "ssh_host not set"}), 400

    ok, _, err = _http_json(f"http://{host}:8080/api/live/start", method="POST", body={})
    if not ok:
        return jsonify({"ok": False, "error": f"node live/start failed: {err or 'unknown'}"}), 502
    return jsonify({"ok": True}), 200

@app.route("/action/secure/preview/stop", methods=["POST"])
def preview_stop():
    data = request.get_json(silent=True) or {}
    cam = (data.get("camera_id") or "").strip()
    if not cam: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam)
    host = ep.get("ssh_host") or ""
    if not host: return jsonify({"ok": False, "error": "ssh_host not set"}), 400

    ok, _, err = _http_json(f"http://{host}:8080/api/live/stop", method="POST", body={})
    if not ok:
        return jsonify({"ok": False, "error": f"node live/stop failed: {err or 'unknown'}"}), 502
    return jsonify({"ok": True}), 200

# -------------------- ADMIN TOOLS --------------------

@app.route("/admin/tools")
def admin_tools_page():
    cams = list_camera_ids()
    endpoints = {c: get_camera_endpoint(c) for c in cams}
    return render_template("admin_tools.html", cameras=cams, endpoints=endpoints, title="Admin Tools")

@app.route("/action/secure/node/status", methods=["POST"])
def node_status():
    data = request.get_json(silent=True) or {}
    cam = (data.get("camera_id") or "").strip()
    if not cam: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam)
    host = ep.get("ssh_host") or ""
    user = ep.get("ssh_user") or "pi"
    if not host: return jsonify({"ok": False, "error": "ssh_host not set"}), 400
    p = _ssh(["ssh", f"{user}@{host}", f"systemctl is-active {ep.get('service_name') or 'camera-node'}"])
    state = (p.stdout or p.stderr).strip()
    return jsonify({"ok": True, "state": state}), 200

@app.route("/action/secure/node/restart", methods=["POST"])
def node_restart():
    data = request.get_json(silent=True) or {}
    cam = (data.get("camera_id") or "").strip()
    if not cam: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam)
    host = ep.get("ssh_host") or ""
    user = ep.get("ssh_user") or "pi"
    svc  = ep.get("service_name") or "camera-node"
    if not host: return jsonify({"ok": False, "error": "ssh_host not set"}), 400
    p = _ssh(["ssh", f"{user}@{host}", f"sudo systemctl restart {svc} && systemctl is-active {svc}"])
    state = (p.stdout or p.stderr).strip()
    return jsonify({"ok": True, "state": state}), 200

@app.route("/action/secure/node/logs", methods=["POST"])
def node_logs():
    data = request.get_json(silent=True) or {}
    cam = (data.get("camera_id") or "").strip()
    lines = int(data.get("lines", 80)); lines = max(10, min(lines, 500))
    if not cam: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam)
    host = ep.get("ssh_host") or ""
    user = ep.get("ssh_user") or "pi"
    svc  = ep.get("service_name") or "camera-node"
    if not host: return jsonify({"ok": False, "error": "ssh_host not set"}), 400
    p = _ssh(["ssh", f"{user}@{host}", f"journalctl -u {svc} --no-pager -n {lines} --output=short"])
    out = (p.stdout or p.stderr).strip()
    return jsonify({"ok": True, "log": out}), 200

@app.route("/action/secure/node/backup_config", methods=["POST"])
def node_backup_config():
    data = request.get_json(silent=True) or {}
    cam = (data.get("camera_id") or "").strip()
    if not cam: return jsonify({"ok": False, "error": "camera_id required"}), 400
    ep = get_camera_endpoint(cam)
    host = ep.get("ssh_host") or ""
    user = ep.get("ssh_user") or "pi"
    cfgp = ep.get("config_path") or "/home/pi/camera_node/config.yaml"
    if not host: return jsonify({"ok": False, "error": "ssh_host not set"}), 400
    ok, text = _ssh_cat(user, host, cfgp)
    if not ok: return jsonify({"ok": False, "error": text.strip() or "read failed"}), 500
    resp = make_response(text)
    resp.headers["Content-Type"] = "text/yaml"
    fn = f"{cam}_config_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.yaml"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fn}"'
    return resp

# -------------------- Startup --------------------
init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
