#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import sys
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(".").resolve()
SKIP_DIRS = {".git", "node_modules"}
TOOL_PREFIXES = (".github/", "scripts/", "tools/")
SKIP_FILES = {"LOCALIZATION_REPORT.json"}
TEXT_RUNTIME_EXTS = {".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json", ".svg", ".xml", ".txt"}

RESOURCE_TAG_RE = re.compile(
    r'<(script|img|link|video|audio|source|iframe)\b[^>]*?\b(src|href|poster)\s*=\s*["\']([^"\']+)["\']',
    re.I,
)
SRCSET_RE = re.compile(r'\bsrcset\s*=\s*["\']([^"\']+)["\']', re.I)
ANCHOR_RE = re.compile(r'<a\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\']', re.I)
CSS_URL_RE = re.compile(r'url\(\s*["\']?([^)"\']+)["\']?\s*\)', re.I)
CSS_IMPORT_RE = re.compile(r'@import\s+(?:url\()?\s*["\']?([^)"\'\s;]+)', re.I)
REMOTE_RE = re.compile(r'^(?:https?:)?//', re.I)
MELIUS_RE = re.compile(r'https?://(?:www\.)?melius\.com', re.I)

def iter_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        yield p, rel

FILES = {rel for _, rel in iter_files()}

def clean_value(v: str) -> str:
    v = v.strip()
    if not v:
        return ""
    if v.startswith(("data:", "blob:", "mailto:", "tel:", "javascript:", "#")):
        return ""
    return v

def resolve_local(from_rel: str, value: str) -> list[str]:
    value = clean_value(value)
    if not value or REMOTE_RE.match(value):
        return []
    split = urlsplit(value)
    path = unquote(split.path)
    if not path:
        return []
    if path == "/":
        return ["index.html"]
    if path.startswith("/"):
        base = Path(path.lstrip("/"))
    else:
        base = Path(from_rel).parent / path
    normalized = Path(os.path.normpath(str(base))).as_posix()
    if normalized in {"", "."}:
        return ["index.html"]
    candidates = [normalized]
    if normalized.endswith("/"):
        candidates.append(normalized + "index.html")
    elif not Path(normalized).suffix:
        candidates += [normalized + ".html", normalized + "/index.html"]
    return list(dict.fromkeys(candidates))

def first_existing(from_rel: str, value: str) -> str | None:
    candidates = resolve_local(from_rel, value)
    if not candidates:
        return None
    return next((c for c in candidates if c in FILES), None)

def exists_local(from_rel: str, value: str) -> bool:
    candidates = resolve_local(from_rel, value)
    return not candidates or any(c in FILES for c in candidates)

errors: list[str] = []
checked_refs = 0
html_count = 0
reachable_css: set[str] = set()

# No live Melius origin may remain in deployable runtime files.
for p, rel in iter_files():
    if Path(rel).name in SKIP_FILES or rel.startswith(TOOL_PREFIXES):
        continue
    if p.suffix.lower() not in TEXT_RUNTIME_EXTS:
        continue
    try:
        text = p.read_text("utf-8", errors="ignore")
    except Exception:
        continue
    if MELIUS_RE.search(text):
        errors.append(f"LIVE ORIGIN STRING in runtime file: {rel}")

# HTML references and local navigation.
for p, rel in iter_files():
    if p.suffix.lower() not in {".html", ".htm"}:
        continue
    html_count += 1
    text = p.read_text("utf-8", errors="ignore")

    for m in RESOURCE_TAG_RE.finditer(text):
        tag, attr, value = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
        if REMOTE_RE.match(value):
            errors.append(f"REMOTE RESOURCE {tag}.{attr}: {rel} -> {value}")
            continue
        checked_refs += 1
        target = first_existing(rel, value)
        if not exists_local(rel, value):
            errors.append(f"MISSING RESOURCE {tag}.{attr}: {rel} -> {value}")
        elif tag == "link" and target and target.endswith(".css"):
            reachable_css.add(target)

    for m in SRCSET_RE.finditer(text):
        for item in m.group(1).split(","):
            value = item.strip().split()[0] if item.strip() else ""
            if not value:
                continue
            if REMOTE_RE.match(value):
                errors.append(f"REMOTE SRCSET: {rel} -> {value}")
                continue
            checked_refs += 1
            if not exists_local(rel, value):
                errors.append(f"MISSING SRCSET: {rel} -> {value}")

    for m in ANCHOR_RE.finditer(text):
        value = m.group(1).strip()
        if not value or REMOTE_RE.match(value) or value.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        checked_refs += 1
        if not exists_local(rel, value):
            errors.append(f"MISSING LOCAL LINK: {rel} -> {value}")

# Follow only CSS that the HTML actually loads, then recurse through local @imports.
queue = deque(sorted(reachable_css))
seen_css: set[str] = set()
while queue:
    rel = queue.popleft()
    if rel in seen_css:
        continue
    seen_css.add(rel)
    p = ROOT / rel
    if not p.exists():
        errors.append(f"MISSING REACHABLE CSS FILE: {rel}")
        continue
    text = p.read_text("utf-8", errors="ignore")

    for m in CSS_IMPORT_RE.finditer(text):
        value = m.group(1).strip()
        if not value or value.startswith(("data:", "blob:", "#")):
            continue
        if REMOTE_RE.match(value):
            errors.append(f"REMOTE CSS IMPORT: {rel} -> {value}")
            continue
        checked_refs += 1
        target = first_existing(rel, value)
        if not target:
            errors.append(f"MISSING CSS IMPORT: {rel} -> {value}")
        elif target.endswith(".css") and target not in seen_css:
            queue.append(target)

    for m in CSS_URL_RE.finditer(text):
        value = m.group(1).strip()
        if not value or value.startswith(("data:", "blob:", "#")):
            continue
        if REMOTE_RE.match(value):
            errors.append(f"REMOTE CSS URL: {rel} -> {value}")
            continue
        checked_refs += 1
        if not exists_local(rel, value):
            errors.append(f"MISSING CSS URL: {rel} -> {value}")

print(f"Static verification: {len(FILES)} Git files, {html_count} HTML files, {len(seen_css)} reachable CSS files, {checked_refs} references checked.")

if errors:
    print(f"FAILED with {len(errors)} issue(s).")
    for e in errors[:400]:
        print(e)
    if len(errors) > 400:
        print(f"... {len(errors) - 400} more")
    sys.exit(1)

print("PASSED: all runtime resources are Git-local, all checked local references resolve, and no deployable runtime file contains the live Melius origin.")
