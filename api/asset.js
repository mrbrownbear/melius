const { Readable } = require("node:stream");
const pathLib = require("node:path");
const manifest = require("../site-manifest.json");

const OWNER = manifest.source.owner;
const REPO = manifest.source.repo;
const REF = manifest.source.ref;
const FILES = manifest.files;
const RAW_BASE = "https://raw.githubusercontent.com/" + OWNER + "/" + REPO + "/" + REF + "/";
const CDN_BASE = "https://cdn.jsdelivr.net/gh/" + OWNER + "/" + REPO + "@" + REF + "/";

function emit(level, event, data = {}) {
  const rec = {
    scope: "melius-asset",
    event,
    ts: new Date().toISOString(),
    source: OWNER + "/" + REPO + "@" + REF.slice(0, 12),
    ...data
  };
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
  if (parts.some(part => part === "." || part === ".." || part.includes("\0"))) {
    throw new Error("unsafe path");
  }
  return parts.join("/");
}

function encodePath(p) {
  return p.split("/").map(part => encodeURIComponent(part)).join("/");
}

function mimeFor(p, upstreamType) {
  const ext = pathLib.extname(p).toLowerCase();
  const table = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
    ".dmg": "application/x-apple-diskimage"
  };
  if (table[ext]) return table[ext];
  if (p.endsWith("/gsi/client")) return "text/javascript; charset=utf-8";
  return upstreamType || "application/octet-stream";
}

async function fetchOrigin(url, req, requestId, origin, assetPath) {
  const headers = {
    "User-Agent": "melius-vercel-asset-proxy/3.0",
    "Accept": "*/*"
  };
  if (req.headers.range) headers.Range = req.headers.range;
  if (req.headers["if-none-match"]) headers["If-None-Match"] = req.headers["if-none-match"];
  if (req.headers["if-modified-since"]) headers["If-Modified-Since"] = req.headers["if-modified-since"];

  const started = Date.now();
  try {
    const response = await fetch(url, {
      method: req.method === "HEAD" ? "HEAD" : "GET",
      redirect: "follow",
      headers
    });
    emit(response.ok || response.status === 206 || response.status === 304 ? "info" : "warn", "origin_response", {
      requestId,
      path: assetPath,
      origin,
      status: response.status,
      range: req.headers.range || null,
      contentType: response.headers.get("content-type"),
      contentLength: response.headers.get("content-length"),
      elapsedMs: Date.now() - started
    });
    return response;
  } catch (error) {
    emit("error", "origin_exception", {
      requestId,
      path: assetPath,
      origin,
      message: error.message,
      elapsedMs: Date.now() - started
    });
    return null;
  }
}

module.exports = async function handler(req, res) {
  const started = Date.now();
  const requestId = req.headers["x-vercel-id"] || req.headers["x-request-id"] || null;

  if (req.method !== "GET" && req.method !== "HEAD") {
    res.statusCode = 405;
    res.setHeader("Allow", "GET, HEAD");
    return res.end();
  }

  let assetPath;
  try {
    assetPath = cleanPath(req.query.path || "");
  } catch (error) {
    emit("error", "unsafe_path", { requestId, message: error.message });
    res.statusCode = 400;
    return res.end("Bad request");
  }

  const meta = FILES[assetPath];
  if (!meta) {
    emit("warn", "manifest_miss", { requestId, path: assetPath });
    res.statusCode = 404;
    res.setHeader("Content-Type", "text/plain; charset=utf-8");
    return res.end("Localized asset not found: " + assetPath);
  }

  emit("info", "asset_request", {
    requestId,
    path: assetPath,
    bytes: meta.size || null,
    range: req.headers.range || null
  });

  const encoded = encodePath(assetPath);
  const origins = [
    { name: "github-raw", url: RAW_BASE + encoded },
    { name: "jsdelivr", url: CDN_BASE + encoded }
  ];

  let response = null;
  let selected = null;
  for (const origin of origins) {
    const candidate = await fetchOrigin(origin.url, req, requestId, origin.name, assetPath);
    if (candidate && (candidate.ok || candidate.status === 206 || candidate.status === 304)) {
      response = candidate;
      selected = origin.name;
      break;
    }
  }

  if (!response) {
    emit("error", "asset_unavailable", {
      requestId,
      path: assetPath,
      elapsedMs: Date.now() - started
    });
    res.statusCode = 502;
    res.setHeader("Content-Type", "text/plain; charset=utf-8");
    return res.end("Localized asset source unavailable: " + assetPath);
  }

  res.statusCode = response.status;
  res.setHeader("Content-Type", mimeFor(assetPath, response.headers.get("content-type")));
  res.setHeader(
    "Cache-Control",
    assetPath.startsWith("_next/")
      ? "public, max-age=31536000, immutable"
      : "public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800"
  );
  res.setHeader("X-Melius-Asset", assetPath);
  res.setHeader("X-Melius-Origin", selected);
  res.setHeader("Access-Control-Allow-Origin", "*");

  for (const header of ["content-range", "accept-ranges", "etag", "last-modified"]) {
    const value = response.headers.get(header);
    if (value) res.setHeader(header.split("-").map(x => x.charAt(0).toUpperCase() + x.slice(1)).join("-"), value);
  }

  // IMPORTANT: do not forward upstream Content-Length here.
  // Node fetch may transparently decompress gzip/br responses while keeping the
  // upstream compressed byte length in the headers. Forwarding that stale length
  // truncates JS/CSS in the browser and causes SyntaxError/Unexpected end of input.
  res.removeHeader("Content-Length");

  if (req.method === "HEAD" || response.status === 304 || !response.body) {
    emit("info", "asset_served", {
      requestId,
      path: assetPath,
      origin: selected,
      status: response.status,
      streamed: false,
      elapsedMs: Date.now() - started
    });
    return res.end();
  }

  emit("info", "asset_served", {
    requestId,
    path: assetPath,
    origin: selected,
    status: response.status,
    streamed: true,
    range: req.headers.range || null,
    elapsedMs: Date.now() - started
  });

  const ext = pathLib.extname(assetPath).toLowerCase();
  const bufferSafe = new Set([
    ".js", ".mjs", ".css", ".json", ".xml", ".txt", ".svg",
    ".woff", ".woff2", ".ttf", ".otf", ".wasm"
  ]);

  if (bufferSafe.has(ext) || assetPath.endsWith("/gsi/client")) {
    try {
      const body = Buffer.from(await response.arrayBuffer());
      res.removeHeader("Content-Length");
      res.setHeader("Content-Length", String(body.length));
      emit("info", "asset_buffered", {
        requestId,
        path: assetPath,
        origin: selected,
        status: response.status,
        bytes: body.length,
        elapsedMs: Date.now() - started
      });
      return res.end(body);
    } catch (error) {
      emit("error", "buffer_error", {
        requestId,
        path: assetPath,
        message: error.message,
        elapsedMs: Date.now() - started
      });
      res.statusCode = 502;
      return res.end("Failed to buffer localized asset");
    }
  }

  const stream = Readable.fromWeb(response.body);
  stream.on("error", error => {
    emit("error", "stream_error", {
      requestId,
      path: assetPath,
      message: error.message
    });
    if (!res.headersSent) res.statusCode = 502;
    res.destroy(error);
  });
  stream.pipe(res);
};
