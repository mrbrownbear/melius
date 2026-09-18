const OWNER = process.env.GITHUB_SITE_OWNER || "mrbrownbear";
const REPO = process.env.GITHUB_SITE_REPO || "melius";
const REF = process.env.GITHUB_SITE_REF || "main";
const RAW_BASE = `https://raw.githubusercontent.com/${OWNER}/${REPO}/${REF}/`;

function emit(level, event, data = {}) {
  const payload = {
    scope: "melius-git-proxy",
    event,
    ts: new Date().toISOString(),
    ...data,
  };
  const line = JSON.stringify(payload);
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.log(line);
}

function safePath(input) {
  let value = String(input || "");
  try { value = decodeURIComponent(value); } catch {}
  value = value.replace(/^\/+/, "").replace(/\\/g, "/");
  const parts = value.split("/").filter(Boolean);
  if (parts.some((part) => part === "." || part === ".." || part.includes("\0"))) {
    throw new Error("unsafe path");
  }
  return parts.join("/");
}

function rawUrl(path) {
  const encoded = path.split("/").map((part) => encodeURIComponent(part)).join("/");
  return RAW_BASE + encoded;
}

function hasExtension(path) {
  const last = path.split("/").pop() || "";
  return /\.[A-Za-z0-9][A-Za-z0-9._-]*$/.test(last);
}

function pageCandidates(path) {
  if (!path) return ["index.html"];
  if (path.endsWith("/")) {
    const base = path.replace(/\/+$/, "");
    return [`${base}/index.html`, `${base}.html`, base];
  }
  if (path.toLowerCase().endsWith(".html")) return [path];
  if (hasExtension(path)) return [path];
  return [path, `${path}.html`, `${path}/index.html`];
}

async function inspectCandidate(path, method = "GET") {
  const url = rawUrl(path);
  const response = await fetch(url, {
    method,
    redirect: "follow",
    headers: {
      "User-Agent": "melius-vercel-git-proxy/1.0",
      "Accept": "*/*",
    },
  });
  return { path, url, response };
}

module.exports = async function handler(req, res) {
  const started = Date.now();
  const requestId = req.headers["x-vercel-id"] || req.headers["x-request-id"] || null;

  let path;
  try {
    path = safePath(req.query.path ?? req.url?.split("?")[0] ?? "");
  } catch (error) {
    emit("error", "unsafe_path", { requestId, input: req.query.path, message: error.message });
    res.statusCode = 400;
    res.setHeader("Content-Type", "text/plain; charset=utf-8");
    return res.end("Bad request");
  }

  if (path === "__health") {
    emit("info", "health", { requestId, owner: OWNER, repo: REPO, ref: REF });
    res.statusCode = 200;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(JSON.stringify({ ok: true, source: `${OWNER}/${REPO}@${REF}` }));
  }

  const candidates = pageCandidates(path);
  emit("info", "request_start", {
    requestId,
    method: req.method,
    path,
    candidates,
    userAgent: req.headers["user-agent"] || null,
  });

  const isLikelyAsset = !!path && hasExtension(path) && !path.toLowerCase().endsWith(".html");

  try {
    if (isLikelyAsset) {
      const candidate = candidates[0];
      const checked = await inspectCandidate(candidate, "HEAD");
      emit("info", "asset_probe", {
        requestId,
        path,
        candidate,
        status: checked.response.status,
        contentType: checked.response.headers.get("content-type"),
        contentLength: checked.response.headers.get("content-length"),
        upstreamUrl: checked.response.url,
        elapsedMs: Date.now() - started,
      });

      if (!checked.response.ok) {
        res.statusCode = checked.response.status === 404 ? 404 : 502;
        res.setHeader("Content-Type", "text/plain; charset=utf-8");
        return res.end(`Asset unavailable: ${candidate}`);
      }

      res.statusCode = 307;
      res.setHeader("Location", rawUrl(candidate));
      res.setHeader("Cache-Control", "public, max-age=300, s-maxage=3600");
      return res.end();
    }

    const attempts = [];
    for (const candidate of candidates) {
      const checked = await inspectCandidate(candidate, "GET");
      attempts.push({ candidate, status: checked.response.status });

      emit("info", "page_probe", {
        requestId,
        path,
        candidate,
        status: checked.response.status,
        contentType: checked.response.headers.get("content-type"),
        contentLength: checked.response.headers.get("content-length"),
        elapsedMs: Date.now() - started,
      });

      if (!checked.response.ok) continue;

      const body = Buffer.from(await checked.response.arrayBuffer());
      let contentType = checked.response.headers.get("content-type") || "application/octet-stream";
      const prefix = body.subarray(0, 256).toString("utf8").toLowerCase();
      if (candidate.toLowerCase().endsWith(".html") || prefix.includes("<!doctype html") || prefix.includes("<html")) {
        contentType = "text/html; charset=utf-8";
      }

      emit("info", "page_selected", {
        requestId,
        path,
        candidate,
        status: checked.response.status,
        bytes: body.length,
        contentType,
        elapsedMs: Date.now() - started,
      });

      res.statusCode = 200;
      res.setHeader("Content-Type", contentType);
      res.setHeader("Cache-Control", contentType.startsWith("text/html")
        ? "public, max-age=0, s-maxage=60, stale-while-revalidate=300"
        : "public, max-age=300, s-maxage=3600");
      return res.end(body);
    }

    emit("warn", "not_found", {
      requestId,
      path,
      attempts,
      elapsedMs: Date.now() - started,
    });
    res.statusCode = 404;
    res.setHeader("Content-Type", "text/plain; charset=utf-8");
    return res.end("Not found");
  } catch (error) {
    emit("error", "proxy_failure", {
      requestId,
      path,
      candidates,
      name: error.name,
      message: error.message,
      stack: error.stack,
      elapsedMs: Date.now() - started,
    });
    res.statusCode = 502;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(JSON.stringify({
      error: "Git-backed site proxy failed",
      path,
      message: error.message,
      requestId,
    }));
  }
};
