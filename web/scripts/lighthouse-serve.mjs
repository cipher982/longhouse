// Serves web/dist for Lighthouse CI (lighthouserc.json `startServerCommand`) the way a Longhouse server does:
// the built files, the SPA fallback, and the runtime /config.js. A static dist has no config.js (the server writes it,
// serve_config_js in server/zerg/main.py), and without it the app throws before it renders, so Lighthouse sees no
// paint (NO_FCP, Provider Nightly 2026-11-27). The values are the ones a server with auth on serves, which renders
// the landing page.
import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";

const DIST = fileURLToPath(new URL("../dist/", import.meta.url));
const PORT = Number(process.env.LIGHTHOUSE_PORT || 4319);
const CONFIG_JS = 'window.API_BASE_URL="/api";\nwindow.WS_BASE_URL="";\nwindow.__SINGLE_TENANT__=false;\n';
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript",
  ".mjs": "application/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml",
};

function fileFor(pathname) {
  const path = normalize(join(DIST, decodeURIComponent(pathname)));
  if (!path.startsWith(DIST.endsWith(sep) ? DIST : DIST + sep)) return null;
  try {
    return statSync(path).isFile() ? path : null;
  } catch {
    return null;
  }
}

createServer((request, response) => {
  const { pathname } = new URL(request.url || "/", "http://localhost");
  if (pathname === "/config.js") {
    response.writeHead(200, { "content-type": "application/javascript", "cache-control": "no-store" });
    response.end(CONFIG_JS);
    return;
  }
  if (pathname.startsWith("/api/")) {
    response.writeHead(404, { "content-type": "application/json" });
    response.end('{"detail":"no server behind the Lighthouse run"}');
    return;
  }
  const path = fileFor(pathname) || join(DIST, "index.html");
  response.writeHead(200, { "content-type": TYPES[extname(path)] || "application/octet-stream" });
  createReadStream(path).pipe(response);
}).listen(PORT, "127.0.0.1", () => {
  console.log(`lighthouse-serve: ready on http://localhost:${PORT}`);
});
