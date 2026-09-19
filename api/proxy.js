const manifest = require("../site-manifest.json");

const OWNER = manifest.source.owner;
const REPO = manifest.source.repo;
const REF = manifest.source.ref;
const CDN_BASE = "https://cdn.jsdelivr.net/gh/" + OWNER + "/" + REPO + "@" + REF + "/";
const RAW_BASE = "https://raw.githubusercontent.com/" + OWNER + "/" + REPO + "/" + REF + "/";
const GITHUB_RAW_BASE = "https://github.com/" + OWNER + "/" + REPO + "/raw/" + REF + "/";
const FILES = new Set(Object.keys(manifest.files));
const PAGES = manifest.pages;

function emit(level, event, data) {
  const rec = Object.assign({
    scope: "melius-site",
    event,
    ts: new Date().toISOString(),
    source: OWNER + "/" + REPO + "@" + REF.slice(0, 12)
  }, data || {});
  const line = JSON.stringify(rec);
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.log(line);
}

function cleanPath(value) {
  let p = String(value || "");
  try { p = decodeURIComponent(p); } catch {}
  p = p.split("?")[0].split("#")[0];
  p = p.replace(/\\/g, "/").replace(/^\/+/, "");
  const parts = p.split("/").filter(Boolean);
  if (parts.some((part) => part === "." || part === ".." || part.indexOf("\0") !== -1)) {
    throw new Error("unsafe path");
  }
  return parts.join("/");
}

function encodePath(p) {
  return p.split("/").map((part) => encodeURIComponent(part)).join("/");
}

function cdnUrl(p) {
  return CDN_BASE + encodePath(p);
}

function rawUrl(p) {
  return RAW_BASE + encodePath(p);
}

function githubRawUrl(p) {
  return GITHUB_RAW_BASE + encodePath(p);
}

function routeKey(p) {
  if (!p) return "/";
  return "/" + p.replace(/\/+$/, "");
}

function prepareHtmlForRuntime(html, requestId) {
  const guardPattern = /<script\s+id=["']__local_only_guard["'][^>]*>[\s\S]*?<\/script>/i;
  const hadGuard = guardPattern.test(html);
  let out = html.replace(guardPattern, "");

  const reporter = `<script id="__runtime_error_reporter">
(function(){
  function send(kind, payload) {
    try {
      var body = JSON.stringify({
        kind: kind,
        href: location.href,
        userAgent: navigator.userAgent,
        payload: payload || null,
        ts: new Date().toISOString()
      });
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/__client_error', new Blob([body], {type:'application/json'}));
      } else {
        fetch('/__client_error', {method:'POST',headers:{'Content-Type':'application/json'},body:body,keepalive:true});
      }
    } catch (_) {}
  }
  window.addEventListener('error', function(e) {
    send('error', {
      message: e.message || null,
      filename: e.filename || null,
      lineno: e.lineno || null,
      colno: e.colno || null,
      stack: e.error && e.error.stack ? e.error.stack : null
    });
  });
  window.addEventListener('unhandledrejection', function(e) {
    var reason = e.reason;
    send('unhandledrejection', {
      message: reason && reason.message ? reason.message : String(reason),
      stack: reason && reason.stack ? reason.stack : null
    });
  });
})();</script>`;

  if (!out.includes('__runtime_error_reporter')) {
    out = out.replace(/<\/head>/i, reporter + "</head>");
  }

  emit("info", "html_runtime_prepare", {
    requestId,
    removedLocalOnlyGuard: hadGuard,
    reporterInjected: true
  });

  return out;
}

async function fetchPage(sourcePath, requestId) {
  const origins = [
    { name: "jsdelivr", url: cdnUrl(sourcePath) },
    { name: "github-raw", url: rawUrl(sourcePath) }
  ];

  for (const origin of origins) {
    const started = Date.now();
    try {
      const response = await fetch(origin.url, {
        redirect: "follow",
        headers: {
          "User-Agent": "melius-vercel-page-proxy/2.0",
          "Accept": "text/html,*/*"
        }
      });
      emit(response.ok ? "info" : "warn", "page_origin", {
        requestId,
        sourcePath,
        origin: origin.name,
        status: response.status,
        elapsedMs: Date.now() - started
      });
      if (response.ok) return { response, origin: origin.name };
    } catch (error) {
      emit("error", "page_origin_exception", {
        requestId,
        sourcePath,
        origin: origin.name,
        message: error.message,
        elapsedMs: Date.now() - started
      });
    }
  }
  return null;
}

module.exports = async function handler(req, res) {
  const started = Date.now();
  const requestId = req.headers["x-vercel-id"] || req.headers["x-request-id"] || null;

  let path;
  try {
    path = cleanPath(req.query.path || "");
  } catch (error) {
    emit("error", "unsafe_path", { requestId, message: error.message });
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(JSON.stringify({ error: "unsafe path", requestId }));
  }

  if (path === "__health") {
    const samples = [
      "_next/static/immutable/chunks/0--fcpug0svxq.js",
      "_next/static/immutable/chunks/0d7g7lz9mwpqx.css",
      "_next/static/immutable/media/797e433ab948586e-s.p.1v5bejj26fx9h.woff2",
      "media/customers/ink/classpass.svg"
    ];
    const checks = [];

    for (const sample of samples) {
      const url = cdnUrl(sample);
      const startedProbe = Date.now();
      try {
        const response = await fetch(url, { method: "HEAD", redirect: "follow" });
        checks.push({
          path: sample,
          status: response.status,
          ok: response.ok,
          contentType: response.headers.get("content-type"),
          contentLength: response.headers.get("content-length"),
          elapsedMs: Date.now() - startedProbe
        });
      } catch (error) {
        checks.push({
          path: sample,
          status: 0,
          ok: false,
          error: error.message,
          elapsedMs: Date.now() - startedProbe
        });
      }
    }

    const ok = checks.every((check) => check.ok);
    const body = {
      ok,
      source: OWNER + "/" + REPO + "@" + REF,
      files: manifest.counts.files,
      pages: manifest.counts.pages,
      assetOrigin: "vercel-same-origin-proxy",
      checks
    };

    emit(ok ? "info" : "error", ok ? "health" : "health_failed", Object.assign({ requestId }, body));
    res.statusCode = ok ? 200 : 503;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("Cache-Control", "no-store");
    return res.end(JSON.stringify(body));
  }

  if (path === "__debug") {
    const requested = cleanPath(req.query.file || "");
    const key = routeKey(requested);
    const page = PAGES[key] || null;
    const fileExists = FILES.has(requested);
    let probe = null;
    if (fileExists) {
      const probeStarted = Date.now();
      try {
        const response = await fetch(cdnUrl(requested), { method: "HEAD", redirect: "follow" });
        probe = {
          status: response.status,
          ok: response.ok,
          contentType: response.headers.get("content-type"),
          contentLength: response.headers.get("content-length"),
          elapsedMs: Date.now() - probeStarted
        };
      } catch (error) {
        probe = {
          status: 0,
          ok: false,
          error: error.message,
          elapsedMs: Date.now() - probeStarted
        };
      }
    }

    const body = {
      requested,
      route: key,
      page,
      fileExists,
      assetUrl: fileExists ? cdnUrl(requested) : null,
      probe
    };
    emit(probe && !probe.ok ? "error" : "info", probe && !probe.ok ? "debug_probe_failed" : "debug_lookup", Object.assign({ requestId }, body));
    res.statusCode = 200;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("Cache-Control", "no-store");
    return res.end(JSON.stringify(body));
  }

  if (path === "__client_error" && req.method === "POST") {
    let raw = "";
    req.on("data", chunk => {
      if (raw.length < 65536) raw += chunk.toString("utf8");
    });
    req.on("end", () => {
      let payload = raw;
      try { payload = JSON.parse(raw || "{}"); } catch {}
      emit("error", "client_runtime_error", {
        requestId,
        payload
      });
      res.statusCode = 204;
      res.setHeader("Cache-Control", "no-store");
      res.end();
    });
    return;
  }

  if (req.method !== "GET" && req.method !== "HEAD") {
    emit("warn", "unsupported_method", { requestId, method: req.method, path });
    res.statusCode = 405;
    res.setHeader("Allow", "GET, HEAD, POST");
    return res.end();
  }

  const route = routeKey(path);
  const pageSource = PAGES[route];

  if (pageSource) {
    emit("info", "page_request", { requestId, path, route, pageSource });
    const result = await fetchPage(pageSource, requestId);

    if (!result) {
      emit("error", "page_unavailable", {
        requestId,
        path,
        route,
        pageSource,
        elapsedMs: Date.now() - started
      });
      res.statusCode = 502;
      res.setHeader("Content-Type", "application/json; charset=utf-8");
      return res.end(JSON.stringify({
        error: "page source unavailable",
        route,
        sourcePath: pageSource,
        requestId
      }));
    }

    const originalBuffer = Buffer.from(await result.response.arrayBuffer());
    const preparedHtml = prepareHtmlForRuntime(originalBuffer.toString("utf8"), requestId);
    const buffer = Buffer.from(preparedHtml, "utf8");

    emit("info", "page_served", {
      requestId,
      path,
      route,
      pageSource,
      origin: result.origin,
      originalBytes: originalBuffer.length,
      bytes: buffer.length,
      assetMode: "same-origin",
      localOnlyGuardRemoved: true,
      elapsedMs: Date.now() - started
    });

    res.statusCode = 200;
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.setHeader("Cache-Control", "public, max-age=0, s-maxage=60, stale-while-revalidate=300");
    res.setHeader("X-Melius-Source", pageSource);
    res.setHeader("X-Melius-Origin", result.origin);
    if (req.method === "HEAD") return res.end();
    return res.end(buffer);
  }

  if (FILES.has(path)) {
    const meta = manifest.files[path] || {};
    let location = cdnUrl(path);
    let origin = "jsdelivr";

    if (path === "desktop-app/download.dmg") {
      location = githubRawUrl(path);
      origin = "github-lfs";
    }

    emit("info", "asset_redirect", {
      requestId,
      path,
      bytes: meta.size || null,
      origin,
      location,
      elapsedMs: Date.now() - started
    });

    res.statusCode = 302;
    res.setHeader("Location", location);
    res.setHeader("Cache-Control", "public, max-age=300, s-maxage=3600");
    res.setHeader("X-Melius-Asset-Origin", origin);
    return res.end();
  }

  emit("warn", "manifest_miss", {
    requestId,
    path,
    route,
    method: req.method,
    elapsedMs: Date.now() - started
  });

  res.statusCode = 404;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  return res.end(JSON.stringify({
    error: "path not present in localized Git manifest",
    path,
    route,
    requestId
  }));
};
