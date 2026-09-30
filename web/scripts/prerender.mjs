#!/usr/bin/env node
// Prerender the public marketing routes to static HTML (run by `bun run build`
// after `vite build`).
//
// Search engines, link unfurlers (Slack, X, HN) and LLM crawlers do not run
// JavaScript, and dist/index.html is an empty shell, so they saw a title and
// nothing else. This renders the real React app for each indexable route with
// react-dom/static, splices the markup and the route's own <head> tags into the
// built shell, and writes dist/_prerender/<route>/index.html. main.tsx hydrates
// that DOM in the browser. The Runtime Host serves these pages on the public
// site only (zerg/frontend_pages.py); every other route keeps the shell.
//
// The routes are the <loc> entries of public/sitemap.xml, so a page is either
// listed and prerendered or neither. A route whose page sets no title
// (usePageMeta) did not render its own page, and the build fails.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createServer } from "vite";
import { buildPage, sitemapRoutes } from "./prerender-page.mjs";

const webRoot = path.resolve(import.meta.dirname, "..");
const distDir = path.join(webRoot, "dist");
const outDir = path.join(distDir, "_prerender");

async function main() {
  const shell = readFileSync(path.join(distDir, "index.html"), "utf8");
  const routes = sitemapRoutes(readFileSync(path.join(webRoot, "public/sitemap.xml"), "utf8"));

  const vite = await createServer({
    configFile: path.join(webRoot, "vite.config.ts"),
    mode: "production",
    logLevel: "warn",
    appType: "custom",
    server: { middlewareMode: true, hmr: false, watch: null },
    resolve: {
      alias: [
        { find: /^@xterm\/(xterm|addon-fit)$/, replacement: path.join(webRoot, "scripts/prerender-xterm-stub.mjs") },
      ],
    },
  });
  try {
    const { renderRoute } = await vite.ssrLoadModule("/src/app/prerender.tsx");
    rmSync(outDir, { recursive: true, force: true });
    for (const route of routes) {
      const page = buildPage(shell, route, await renderRoute(route.pathname));
      const file = path.join(outDir, route.pathname.replace(/^\/+|\/+$/g, ""), "index.html");
      mkdirSync(path.dirname(file), { recursive: true });
      writeFileSync(file, page);
      console.log(`prerendered ${route.pathname} (${page.length} bytes)`);
    }
  } finally {
    await vite.close();
  }
}

await main();
