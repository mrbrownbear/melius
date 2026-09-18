#!/usr/bin/env python3
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

if DIST.exists():
    shutil.rmtree(DIST)
DIST.mkdir()

ONLY = {"index.html"}

count = 0
size = 0

for rel_name in sorted(ONLY):
    p = ROOT / rel_name
    if not p.is_file():
        raise SystemExit(f"Missing required file: {rel_name}")
    target = DIST / rel_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, target)
    count += 1
    size += p.stat().st_size

mb = size / 1024 / 1024
print(f"Prepared Vercel diagnostic output: {count} files, {mb:.2f} MB")
