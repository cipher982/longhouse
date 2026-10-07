// Pure pieces of web/scripts/prerender.mjs, kept apart so tests can import them
// without starting Vite.

function escapeAttr(value) {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function replaceOnce(html, pattern, replacement, what) {
  if (!pattern.test(html)) throw new Error(`prerender: dist/index.html has no ${what}`);
  return html.replace(pattern, () => replacement);
}

const metaTag = (attr, key) => new RegExp(`<meta\\s+${attr}="${key}"[^>]*>`);

/** Sitemap <loc> entries as [origin, pathname] pairs. */
export function sitemapRoutes(sitemapXml) {
  const routes = [...sitemapXml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => new URL(match[1].trim()));
  if (routes.length === 0) throw new Error("prerender: sitemap.xml lists no pages");
  return routes.map((url) => ({ origin: url.origin, pathname: url.pathname }));
}

/**
 * The JS and CSS files (as /assets/... hrefs) that the lazy chunks built from
 * `modules` need beyond what the shell already loads, from Vite's manifest.
 */
export function chunkAssets(manifest, modules, shell) {
  const scripts = new Set();
  const styles = new Set();
  const seen = new Set();
  const visit = (key) => {
    if (seen.has(key)) return;
    seen.add(key);
    const chunk = manifest[key];
    if (!chunk) throw new Error(`prerender: the build manifest has no chunk ${key}`);
    if (chunk.file.endsWith(".js")) scripts.add(`/${chunk.file}`);
    for (const css of chunk.css ?? []) styles.add(`/${css}`);
    for (const imported of chunk.imports ?? []) visit(imported);
  };
  for (const module of modules) {
    if (!manifest[module]?.isDynamicEntry) throw new Error(`prerender: ${module} is not a lazily loaded chunk in the build manifest`);
    visit(module);
  }
  const inShell = (href) => shell.includes(`"${href}"`);
  return { scripts: [...scripts].filter((href) => !inShell(href)), styles: [...styles].filter((href) => !inShell(href)) };
}

/** The built shell with one route's markup and head tags. */
export function buildPage(shell, { origin, pathname }, { html, meta }, assets = { scripts: [], styles: [] }) {
  if (!meta.title) throw new Error(`prerender ${pathname}: the page sets no title (usePageMeta); is the route missing from App.tsx?`);
  if (!meta.description) throw new Error(`prerender ${pathname}: the page sets no description (usePageMeta)`);
  const url = `${origin}${pathname}`;
  const title = escapeAttr(meta.title);
  const description = escapeAttr(meta.description);

  let page = shell;
  page = replaceOnce(page, /<title>[^<]*<\/title>/, `<title>${title}</title>`, "<title>");
  page = replaceOnce(page, metaTag("name", "description"), `<meta name="description" content="${description}" />`, "meta description");
  page = replaceOnce(page, metaTag("property", "og:title"), `<meta property="og:title" content="${title}" />`, "og:title");
  page = replaceOnce(page, metaTag("property", "og:description"), `<meta property="og:description" content="${description}" />`, "og:description");
  page = replaceOnce(page, metaTag("property", "og:url"), `<meta property="og:url" content="${escapeAttr(url)}" />`, "og:url");
  page = replaceOnce(page, metaTag("name", "twitter:title"), `<meta name="twitter:title" content="${title}" />`, "twitter:title");
  page = replaceOnce(page, metaTag("name", "twitter:description"), `<meta name="twitter:description" content="${description}" />`, "twitter:description");
  // A lazy route's chunks, linked the way Vite links the entry's own (its
  // runtime preload helper skips a stylesheet already in the page), so the
  // page is styled at first paint and main.tsx finds the chunk on its way.
  const links = [
    ...assets.scripts.map((href) => `<link rel="modulepreload" crossorigin href="${escapeAttr(href)}">`),
    ...assets.styles.map((href) => `<link rel="stylesheet" crossorigin href="${escapeAttr(href)}">`),
    `<link rel="canonical" href="${escapeAttr(url)}" />`,
  ];
  page = replaceOnce(page, /<\/head>/, `${links.join("\n  ")}\n  </head>`, "</head>");

  // usePublicPageScroll adds these once the app runs; without them the page
  // cannot scroll before (or without) JavaScript. data-ui-effects is what
  // main.tsx sets ("on") unless the page turns effects off (useRootUiEffects).
  page = replaceOnce(page, /<html lang="en">/, `<html lang="en" class="public-page-scroll">`, "<html lang>");
  page = replaceOnce(page, /<body>/, `<body class="public-page-scroll">`, "<body>");
  page = replaceOnce(page, /<div id="react-root"><\/div>/, `<div id="react-root" data-ui-effects="${meta.uiEffects === false ? "off" : "on"}">${html}</div>`, "empty #react-root");
  return page;
}
