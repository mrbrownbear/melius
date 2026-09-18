#!/usr/bin/env python3
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

ROOT = "https://www.melius.com"
HOSTS = {"www.melius.com", "melius.com"}
ROUTES = [
    "/", "/about", "/blog", "/book-intro", "/brand", "/contact",
    "/desktop-app", "/enterprise", "/manifesto", "/models", "/pricing",
    "/privacy", "/terms",
]
OUT = Path("site")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36"
TRACKER_HOSTS = {
    "static.claydar.com", "cdn.claydar.com", "api.claydar.com",
    "assets.apollo.io", "aplo-evnt.com", "r.melius.com",
    "tag.unifyintent.com", "api.unifyintent.com", "connect.facebook.net",
    "www.facebook.com", "www.google-analytics.com", "www.googletagmanager.com",
    "us.i.posthog.com", "eu.i.posthog.com", "us.posthog.com", "eu.posthog.com",
    "us-assets.i.posthog.com", "eu-assets.i.posthog.com", "va.vercel-scripts.com",
    "vercel.live",
}
RESOURCE_EXTS = {
    ".js", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp",
    ".gif", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".webm",
    ".mp4", ".mov", ".mp3", ".wav", ".pdf", ".bin", ".txt", ".xml",
}
TEXT_EXTS = {".html", ".htm", ".js", ".css", ".json", ".svg", ".txt", ".xml", ".map"}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "*/*"})
url_map: dict[str, str] = {}
seen_urls: set[str] = set()
failed_urls: list[str] = []


def sha10(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:10]


def clean_url(url: str) -> str:
    p = urlparse(url)
    return p._replace(fragment="").geturl()


def is_http(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def is_tracker(url: str) -> bool:
    try:
        return urlparse(url).hostname in TRACKER_HOSTS
    except Exception:
        return False


def local_path_for(url: str, content_type: str = "") -> str | None:
    url = clean_url(url)
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        return None
    host = (p.hostname or "").lower()
    path = unquote(p.path or "/")

    if host in HOSTS and path == "/_next/image":
        qs = parse_qs(p.query)
        raw = qs.get("url", [""])[0]
        if raw:
            original = urljoin(ROOT, raw)
            return local_path_for(original, content_type)

    if not path or path.endswith("/"):
        path += "index"
    suffix = Path(path).suffix
    if not suffix:
        guess = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip()) or ""
        if guess in {".jpe", ".jpeg"}:
            guess = ".jpg"
        path += guess
        suffix = guess

    if p.query:
        stem = str(Path(path).with_suffix(""))
        ext = Path(path).suffix
        path = f"{stem}__q_{sha10(p.query)}{ext}"

    if host in HOSTS:
        rel = path.lstrip("/")
    else:
        rel = f"_external/{host}/{path.lstrip('/')}"
    return "/" + rel


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_bytes(rel: str, data: bytes) -> None:
    dest = OUT / rel.lstrip("/")
    ensure_parent(dest)
    dest.write_bytes(data)


def fetch_asset(url: str, force: bool = False) -> str | None:
    url = clean_url(url)
    if url in seen_urls and not force:
        return url_map.get(url)
    seen_urls.add(url)
    if not is_http(url) or is_tracker(url):
        return None
    pcheck = urlparse(url)
    if pcheck.hostname not in HOSTS and Path(pcheck.path).suffix.lower() not in RESOURCE_EXTS:
        return None
    try:
        p = urlparse(url)
        if p.hostname in HOSTS and p.path == "/_next/image":
            raw = parse_qs(p.query).get("url", [""])[0]
            if raw:
                original = urljoin(ROOT, raw)
                rel = fetch_asset(original)
                if rel:
                    url_map[url] = rel
                    return rel
        r = session.get(url, timeout=20, allow_redirects=True)
        if r.status_code >= 400:
            failed_urls.append(f"{r.status_code} {url}")
            return None
        ctype = r.headers.get("content-type", "")
        rel = local_path_for(url, ctype)
        if not rel:
            return None
        write_bytes(rel, r.content)
        url_map[url] = rel
        if r.url != url:
            url_map[clean_url(r.url)] = rel
        return rel
    except Exception as exc:
        failed_urls.append(f"ERR {url} {exc}")
        return None


def extract_urls_from_text(text: str, base: str = ROOT) -> set[str]:
    found: set[str] = set()
    for m in re.findall(r'''https?://[^\s"'<>\\)]+''', text):
        found.add(m.rstrip(";,]}"))
    for m in re.findall(r'''(?:(?:src|href|poster)\s*=\s*["']|url\(["']?)(/[^"'\s)>]+)''', text, flags=re.I):
        found.add(urljoin(base, m))
    for m in re.findall(r'''["'](/(?:_next/static|media|images|svg)/[^"']+)["']''', text):
        found.add(urljoin(base, m))
    return {clean_url(u) for u in found if is_http(u)}


def route_file(route: str) -> Path:
    if route == "/":
        return OUT / "index.html"
    return OUT / f"{route.strip('/')}.html"


RUNTIME = r'''<script id="__local_only_guard">
(() => {
  const LOCAL = location.origin;
  const same = (u) => {
    try { const x = new URL(String(u && u.url ? u.url : u), location.href); return !/^https?:$/.test(x.protocol) || x.origin === LOCAL; }
    catch { return true; }
  };
  const localize = (u) => {
    try {
      const raw = String(u && u.url ? u.url : u);
      const x = new URL(raw, location.href);
      if (x.origin === LOCAL && x.pathname === "/_next/image") {
        const source = x.searchParams.get("url");
        if (source) return decodeURIComponent(source);
      }
      return raw;
    } catch { return u; }
  };
  const localizeSrcset = (v) => String(v).split(",").map(part => {
    const bits = part.trim().split(/\\s+/);
    if (bits[0]) bits[0] = localize(bits[0]);
    return bits.join(" ");
  }).join(", ");
  const empty = (type="application/json") => Promise.resolve(new Response(type.includes("json") ? "{}" : "", {status:200,headers:{"Content-Type":type}}));
  const f = window.fetch;
  window.fetch = function(u,o){ const x=localize(u); return same(x) ? f.call(this,x,o) : empty(); };
  const xo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m,u,...rest){ const x=localize(u); return xo.call(this,m,same(x)?x:"/api/empty.json",...rest); };
  try { navigator.sendBeacon = () => true; } catch {}
  const attrs = [[HTMLImageElement,"src"],[HTMLScriptElement,"src"],[HTMLLinkElement,"href"],[HTMLVideoElement,"src"],[HTMLAudioElement,"src"],[HTMLSourceElement,"src"],[HTMLIFrameElement,"src"]];
  for (const [C,p] of attrs) {
    try {
      const d = Object.getOwnPropertyDescriptor(C.prototype,p);
      if (d && d.set) Object.defineProperty(C.prototype,p,{...d,set(v){ const x=localize(v); if (!same(x) && this.tagName !== "A") return d.set.call(this, this.tagName === "IMG" ? "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" : ""); return d.set.call(this,x); }});
    } catch {}
  }
  try {
    const d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,"srcset");
    if (d && d.set) Object.defineProperty(HTMLImageElement.prototype,"srcset",{...d,set(v){ return d.set.call(this,localizeSrcset(v)); }});
  } catch {}
  const nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name,value) {
    const n = String(name).toLowerCase();
    if (n === "srcset") value = localizeSrcset(value);
    else if (["src","href","poster"].includes(n) && this.tagName !== "A") value = localize(value);
    return nativeSetAttribute.call(this,name,value);
  };
  document.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    try {
      const u = new URL(a.href, location.href);
      if (u.origin === location.origin) {
        e.preventDefault(); e.stopImmediatePropagation(); location.href = u.href;
      }
    } catch {}
  }, true);
})();
</script>'''


def rewrite_html(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "link", "img", "source", "video", "audio", "iframe"]):
        attr = "href" if tag.name == "link" else "src"
        if tag.name in {"video"} and tag.get("poster"):
            v = tag.get("poster")
            absu = urljoin(page_url, v)
            mapped = url_map.get(clean_url(absu)) or fetch_asset(absu)
            if mapped:
                tag["poster"] = mapped
        v = tag.get(attr)
        if v and is_http(urljoin(page_url, v)):
            absu = clean_url(urljoin(page_url, v))
            if is_tracker(absu):
                if tag.name in {"script", "iframe"}:
                    tag.decompose()
                    continue
                tag[attr] = ""
            else:
                mapped = url_map.get(absu) or fetch_asset(absu)
                if mapped:
                    tag[attr] = mapped
                elif urlparse(absu).hostname not in HOSTS:
                    tag[attr] = ""

        if tag.has_attr("srcset"):
            parts = []
            for part in str(tag["srcset"]).split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                absu = clean_url(urljoin(page_url, bits[0]))
                mapped = url_map.get(absu) or fetch_asset(absu)
                if mapped:
                    bits[0] = mapped
                    parts.append(" ".join(bits))
            if parts:
                tag["srcset"] = ", ".join(parts)
            else:
                del tag["srcset"]

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel", [])).lower()
        if any(x in rel for x in ["preconnect", "dns-prefetch"]):
            link.decompose()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        absu = urljoin(page_url, href)
        p = urlparse(absu)
        if p.hostname in HOSTS:
            path = p.path or "/"
            if path != "/" and path.endswith("/"):
                path = path[:-1]
            a["href"] = path + (("?" + p.query) if p.query else "") + (("#" + p.fragment) if p.fragment else "")

    for tag in soup.find_all(True):
        for k, v in list(tag.attrs.items()):
            if not isinstance(v, str):
                continue
            if "https://www.melius.com" in v or "https://melius.com" in v:
                tag.attrs[k] = v.replace("https://www.melius.com", "").replace("https://melius.com", "")

    guard = BeautifulSoup(RUNTIME, "html.parser")
    target = soup.head or soup
    target.insert(0, guard)
    return str(soup)


def patch_text_file(path: Path) -> None:
    try:
        text = path.read_text("utf-8")
    except Exception:
        return

    original = text
    for remote, local in sorted(url_map.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(remote, local)
        if remote.startswith("https://www.melius.com"):
            text = text.replace(remote.replace("https://www.melius.com", "https://melius.com"), local)

    text = text.replace("https://www.melius.com", "").replace("https://melius.com", "")
    text = re.sub(r'https://(?:www\.)?melius\.com(?=[/"\'])', '', text)
    if text != original:
        path.write_text(text, "utf-8")


async def crawl() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "api").mkdir(parents=True, exist_ok=True)
    (OUT / "api/empty.json").write_text("{}", "utf-8")

    captured: dict[str, bytes] = {}
    content_types: dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page = await context.new_page()

        async def on_response(resp):
            u = clean_url(resp.url)
            if not is_http(u) or is_tracker(u):
                return
            try:
                ct = resp.headers.get("content-type", "")
                content_types[u] = ct
                if resp.status < 400 and resp.request.resource_type not in {"document"}:
                    body = await resp.body()
                    captured[u] = body
            except Exception:
                pass

        page.on("response", on_response)

        for route in ROUTES:
            url = ROOT + route
            print(f"CAPTURE {url}", flush=True)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(1200)
                for frac in [0.2, 0.4, 0.6, 0.8, 1.0, 0.0]:
                    await page.evaluate("f => window.scrollTo(0, Math.max(0, document.body.scrollHeight * f))", frac)
                    await page.wait_for_timeout(350)
                html = await page.content()
                route_file(route).write_text(html, "utf-8")
            except Exception as exc:
                print(f"WARN route {route}: {exc}", file=sys.stderr)

        await browser.close()

    for u, data in captured.items():
        rel = local_path_for(u, content_types.get(u, ""))
        if rel:
            write_bytes(rel, data)
            url_map[u] = rel

    for u in list(captured):
        ct = content_types.get(u, "").lower()
        ext = Path(urlparse(u).path).suffix.lower()
        if ext in RESOURCE_EXTS or ct.startswith(("image/", "video/", "audio/", "font/")) or "javascript" in ct or "text/css" in ct:
            fetch_asset(u, force=True)

    queue: set[str] = set()
    for p in OUT.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT_EXTS:
            try:
                queue |= extract_urls_from_text(p.read_text("utf-8", errors="ignore"))
            except Exception:
                pass
    for p in route_file("/").parent.glob("*.html"):
        try:
            queue |= extract_urls_from_text(p.read_text("utf-8", errors="ignore"), ROOT)
        except Exception:
            pass

    rounds = 0
    while queue and rounds < 6:
        rounds += 1
        current = list(queue)
        queue.clear()
        for u in current:
            if is_tracker(u):
                continue
            rel = fetch_asset(u)
            if rel:
                fp = OUT / rel.lstrip("/")
                if fp.suffix.lower() in TEXT_EXTS and fp.exists():
                    try:
                        for nu in extract_urls_from_text(fp.read_text("utf-8", errors="ignore"), u):
                            if nu not in seen_urls:
                                queue.add(nu)
                    except Exception:
                        pass

    for route in ROUTES:
        fp = route_file(route)
        if not fp.exists():
            continue
        page_url = ROOT + route
        fp.write_text(rewrite_html(fp.read_text("utf-8", errors="ignore"), page_url), "utf-8")
        if route != "/":
            folder = OUT / route.strip("/")
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, folder / "index.html")

    for p in OUT.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT_EXTS:
            patch_text_file(p)

    (OUT / ".nojekyll").write_text("", "utf-8")
    (OUT / "vercel.json").write_text(json.dumps({
        "cleanUrls": True,
        "headers": [
            {"source": "/_next/static/(.*)", "headers": [{"key": "Cache-Control", "value": "public, max-age=31536000, immutable"}]},
            {"source": "/(.*).webm", "headers": [{"key": "Cache-Control", "value": "public, max-age=31536000, immutable"}]},
        ],
    }, indent=2), "utf-8")

    report = {
        "routes": ROUTES,
        "saved_files": sum(1 for p in OUT.rglob("*") if p.is_file()),
        "url_map_entries": len(url_map),
        "failed_fetches": failed_urls[:200],
    }
    (OUT / "LOCALIZATION_REPORT.json").write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps(report, indent=2))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


def offline_audit() -> None:
    bad: list[str] = []
    for p in OUT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXTS:
            continue
        try:
            text = p.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        if "https://www.melius.com" in text or "https://melius.com" in text:
            bad.append(f"live origin string: {p}")
        if p.suffix.lower() in {".html", ".htm"}:
            soup = BeautifulSoup(text, "html.parser")
            for tag, attr in [("script","src"),("img","src"),("link","href"),("video","src"),("source","src"),("iframe","src")]:
                for node in soup.find_all(tag):
                    v = node.get(attr)
                    if isinstance(v, str) and is_http(v):
                        bad.append(f"remote {tag}.{attr}: {p}: {v}")
    if bad:
        print("OFFLINE AUDIT FAILED")
        print("\n".join(bad[:100]))
        raise SystemExit(2)
    print("OFFLINE AUDIT PASSED: no live melius origin and no remote resource tags")


if __name__ == "__main__":
    if OUT.exists():
        shutil.rmtree(OUT)
    asyncio.run(crawl())
    offline_audit()
