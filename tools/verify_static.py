#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(".").resolve()
SKIP_DIRS = {".git", "node_modules"}
SKIP_FILES = {"LOCALIZATION_REPORT.json"}
TEXT_RUNTIME_EXTS = {".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json", ".svg", ".xml", ".txt"}
RESOURCE_TAG_RE = re.compile(
    r'<(script|img|link|video|audio|source|iframe)\b[^>]*?\b(src|href|poster)\s*=\s*["\']([^"\']+)["\']',
    re.I,
)
SRCSET_RE = re.compile(r'\bsrcset\s*=\s*["\']([^"\']+)["\']', re.I)
ANCHOR_RE = re.compile(r'<a\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\']', re.I)
CSS_URL_RE = re.compile(r'url\(\s*["\']?([^)"\']+)["\']?\s*\)', re.I)
REMOTE_RE = re.compile(r'^https?://', re.I)
MELIUS_RE = re.compile(r'https?://(?:www\.)?melius\.com', re.I)

def iter_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel.as_posix()

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
    if not value:
        return []
    if REMOTE_RE.match(value):
        return []
    split = urlsplit(value)
    path = unquote(split.path)
    if not path:
        return []
    if path.startswith("/"):
        base = Path(path.lstrip("/"))
    else:
        base = (Path(from_rel).parent / path)
    normalized = Path(os.path.normpath(str(base))).as_posix()
    if normalized == ".":
        normalized = ""
    candidates = [normalized]
    if normalized and not Path(normalized).suffix:
        candidates += [normalized + ".html", normalized.rstrip("/") + "/index.html"]
    elif normalized.endswith("/"):
        candidates += [normalized + "index.html"]
    return list(dict.fromkeys(candidates))

def exists_local(from_rel: str, value: str) -> bool:
    candidates = resolve_local(from_rel, value)
    return not candidates or any(c in FILES for c in candidates)

errors: list[str] = []
checked_refs = 0
html_count = 0
css_count = 0

for p, rel in iter_files():
    if Path(rel).name in SKIP_FILES:
        continue
    ext = p.suffix.lower()
    if ext not in TEXT_RUNTIME_EXTS:
        continue
    try:
        text = p.read_text("utf-8", errors="ignore")
    except Exception:
        continue

    if MELIUS_RE.search(text):
        errors.append(f"LIVE ORIGIN STRING in runtime file: {rel}")

    if ext in {".html", ".htm"}:
        html_count += 1
        for m in RESOURCE_TAG_RE.finditer(text):
            tag, attr, value = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
            if REMOTE_RE.match(value):
                errors.append(f"REMOTE RESOURCE {tag}.{attr}: {rel} -> {value}")
                continue
            checked_refs += 1
            if not exists_local(rel, value):
                errors.append(f"MISSING RESOURCE {tag}.{attr}: {rel} -> {value}")

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

    elif ext == ".css":
        css_count += 1
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

print(f"Static verification: {len(FILES)} files, {html_count} HTML files, {css_count} CSS files, {checked_refs} local references checked.")

if errors:
    print(f"FAILED with {len(errors)} issue(s).")
    for e in errors[:300]:
        print(e)
    if len(errors) > 300:
        print(f"... {len(errors) - 300} more")
    sys.exit(1)

print("PASSED: no live Melius origin in runtime files, no remote resource tags/CSS URLs, and all checked local references resolve to Git files.")
