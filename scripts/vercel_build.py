#!/usr/bin/env python3
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

if DIST.exists():
    shutil.rmtree(DIST)
DIST.mkdir()

EXCLUDE_DIRS = {
    ".git", ".github", ".vercel", "dist", "tools", "scripts",
    "media", "images", "videos", "_external", "_vercel", "api",
    "blog", "models",
}
EXCLUDE_FILES = {
    "vercel.json", ".vercelignore", ".gitattributes", ".nojekyll",
    "about.html", "blog.html", "book-intro.html", "brand.html",
    "contact.html", "desktop-app.html", "enterprise.html",
    "manifesto.html", "models.html", "pricing.html", "privacy.html",
    "terms.html", ".vercel-trigger",
}

count = 0
size = 0

for p in ROOT.rglob("*"):
    if not p.is_file():
        continue
    rel = p.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
        continue
    if rel.name in EXCLUDE_FILES:
        continue
    if rel.suffix.lower() == ".htc":
        continue
    if rel.as_posix() == "desktop-app/download.dmg":
        continue

    target = DIST / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, target)
    count += 1
    size += p.stat().st_size

mb = size / 1024 / 1024
print(f"Prepared Vercel diagnostic output: {count} files, {mb:.2f} MB")
