#!/usr/bin/env python3
import argparse, sqlite3, secrets, sys, yaml

CFG = "/home/pi/hub_server/config.yaml"
try:
    DB = (yaml.safe_load(open(CFG)) or {}).get("database") or "/home/pi/data/hub.db"
except Exception:
    DB = "/home/pi/data/hub.db"

def connect():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS node_tokens(
      node_id TEXT PRIMARY KEY,
      token   TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );""")
    return con

def cmd_add(args):
    t = args.token or secrets.token_hex(16)
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO node_tokens(node_id,token) VALUES(?,?)",(args.node_id,t))
        con.commit()
    print(t)

def cmd_rotate(args):
    t = secrets.token_hex(16)
    with connect() as con:
        cur = con.execute("UPDATE node_tokens SET token=? WHERE node_id=?",(t,args.node_id))
        if cur.rowcount == 0:
            print(f"ERR: node_id '{args.node_id}' not found", file=sys.stderr); sys.exit(1)
        con.commit()
    print(t)

def cmd_show(args):
    with connect() as con:
        cur = con.execute("SELECT token FROM node_tokens WHERE node_id=?",(args.node_id,))
        row = cur.fetchone()
        if not row:
            print(f"ERR: node_id '{args.node_id}' not found", file=sys.stderr); sys.exit(1)
        print(row[0])

def cmd_list(_):
    with connect() as con:
        for nid, tok in con.execute("SELECT node_id, token FROM node_tokens ORDER BY node_id;"):
            print(f"{nid} {tok}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add or replace a node token")
    p_add.add_argument("node_id")
    p_add.add_argument("--token")
    p_add.set_defaults(func=cmd_add)

    p_rot = sub.add_parser("rotate", help="rotate token for existing node")
    p_rot.add_argument("node_id")
    p_rot.set_defaults(func=cmd_rotate)

    p_show = sub.add_parser("show", help="show token")
    p_show.add_argument("node_id")
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="list all tokens")
    p_list.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)
