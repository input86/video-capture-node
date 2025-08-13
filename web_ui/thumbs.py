#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, subprocess, sys
from pathlib import Path
import yaml
from datetime import datetime

HUB_CFG = Path(os.environ.get("HUB_SERVER_CONFIG", str(Path.home()/ "hub_server" / "config.yaml")))
if not HUB_CFG.exists():
    print(f"[thumbs] missing hub config at {HUB_CFG}", file=sys.stderr); sys.exit(1)

cfg = yaml.safe_load(open(HUB_CFG, "r")) or {}
storage = cfg.get("storage", {}) or {}
base_dir = storage.get("base_dir", "/home/pi/data")
clips_subdir = storage.get("clips_subdir", "clips")
CLIPS_DIR = Path(base_dir) / clips_subdir

def pick_timecode(video_path: Path) -> str:
    # simple: use 1s in — good enough for 5s clips
    return "00:00:01"

def make_thumb(video: Path, thumb: Path) -> bool:
    thumb.parent.mkdir(parents=True, exist_ok=True)
    t = pick_timecode(video)
    cmd = ["ffmpeg", "-y", "-ss", t, "-i", str(video), "-frames:v", "1", str(thumb)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def scan_and_build():
    built = 0
    for mp4 in CLIPS_DIR.rglob("*.mp4"):
        jpg = mp4.with_suffix(".jpg")
        if not jpg.exists():
            if make_thumb(mp4, jpg):
                built += 1
    return built

def prune_orphans():
    removed = 0
    for jpg in CLIPS_DIR.rglob("*.jpg"):
        mp4 = jpg.with_suffix(".mp4")
        if not mp4.exists():
            try:
                jpg.unlink()
                removed += 1
            except Exception:
                pass
    return removed

def main():
    do_prune = ("--prune" in sys.argv)
    built = scan_and_build()
    removed = prune_orphans() if do_prune else 0
    print(f"[thumbs] built={built} removed={removed} at {datetime.utcnow().isoformat()}Z")

if __name__ == "__main__":
    main()
