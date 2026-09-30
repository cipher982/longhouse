import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AfterHydration } from "@/features/marketing/AfterHydration";
import { PageMetaCollectorContext, usePageMeta, type CollectedPageMeta } from "@/shared/hooks/usePageMeta";
// @ts-expect-error plain .mjs shared with web/scripts/prerender.mjs
import { buildPage, sitemapRoutes } from "../../../scripts/prerender-page.mjs";
import { renderRoute } from "../prerender";

const SHELL = `<!doctype html>
<html lang="en">
  <head>
    <title>Shell</title>
    <meta
      name="description"
      content="shell description"
    />
    <meta property="og:title" content="Shell" />
    <meta property="og:description" content="shell description" />
    <meta property="og:url" content="https://longhouse.ai" />
    <meta name="twitter:title" content="Shell" />
    <meta name="twitter:description" content="shell description" />
  </head>
  <body>
    <div id="react-root"></div>
  </body>
</html>`;

const SITEMAP = readFileSync(path.resolve(import.meta.dirname, "../../../public/sitemap.xml"), "utf8");
const ROUTES: Array<{ origin: string; pathname: string }> = sitemapRoutes(SITEMAP);

describe("prerender page assembly", () => {
  it("gives each route its own title, description, canonical and OpenGraph tags", () => {
    const page = buildPage(
      SHELL,
      { origin: "https://longhouse.ai", pathname: "/docs/cli" },
      { html: "<h1>CLI</h1>", meta: { title: 'CLI "reference"', description: "Every <command>." } },
    );

    expect(page).toContain('<title>CLI &quot;reference&quot;</title>');
    expect(page).toContain('<meta name="description" content="Every &lt;command&gt;." />');
    expect(page).toContain('<meta property="og:title" content="CLI &quot;reference&quot;" />');
    expect(page).toContain('<meta property="og:url" content="https://longhouse.ai/docs/cli" />');
    expect(page).toContain('<meta name="twitter:description" content="Every &lt;command&gt;." />');
    expect(page).toContain('<link rel="canonical" href="https://longhouse.ai/docs/cli" />');
    expect(page).toContain('<div id="react-root" data-ui-effects="on"><h1>CLI</h1></div>');
    expect(page).toContain('<html lang="en" class="public-page-scroll">');
    expect(page).not.toContain("shell description");
  });

  it("refuses a page that never set its own title", () => {
    expect(() =>
      buildPage(SHELL, { origin: "https://longhouse.ai", pathname: "/nope" }, { html: "", meta: {} }),
    ).toThrow(/sets no title/);
  });

  it("refuses a shell it cannot fill in", () => {
    expect(() =>
      buildPage(
        SHELL.replace('<meta property="og:url" content="https://longhouse.ai" />', ""),
        { origin: "https://longhouse.ai", pathname: "/" },
        { html: "", meta: { title: "t", description: "d" } },
      ),
    ).toThrow(/no og:url/);
  });

  it("collects the page's own title and description while rendering", () => {
    function Page() {
      usePageMeta({ title: "T", description: "D" });
      return null;
    }
    const meta: CollectedPageMeta = {};
    renderToString(
      <PageMetaCollectorContext.Provider value={meta}>
        <Page />
      </PageMetaCollectorContext.Provider>,
    );
    expect(meta).toEqual({ title: "T", description: "D" });
  });

  it("shows the fallback in server HTML and the section in a client render", () => {
    const tree = (
      <AfterHydration fallback={<p>placeholder</p>}>
        <p>demo</p>
      </AfterHydration>
    );
    expect(renderToString(tree)).toContain("placeholder");
    expect(renderToString(tree)).not.toContain("demo");
    render(tree);
    expect(screen.getByText("demo")).toBeInTheDocument();
  });
});

describe("prerendered routes", () => {
  it.each(ROUTES.map((route) => route.pathname))("%s renders its own page", async (pathname) => {
    const { html, meta } = await renderRoute(pathname);

    expect(meta.title, `${pathname} sets no title`).toBeTruthy();
    expect(meta.description, `${pathname} sets no description`).toBeTruthy();
    expect(html).toContain("<h1");
    expect(html).not.toContain("Something went wrong");
  });

  it("the landing page carries the headline and the iOS download, and no claim about live certification", async () => {
    const { html } = await renderRoute("/");

    expect(html).toContain("Remote control for");
    expect(html).toContain("your coding agents.");
    expect(html).toContain("Download on iOS");
    expect(html).toContain("Control support, provider by provider.");
    // Certification is served live; the static page must not say it is unavailable or lit.
    expect(html).not.toMatch(/Certification status|data-certification|is-supported/);
    // Demos mount after hydration, so no demo chrome and no streaming instructions in the HTML.
    expect(html).not.toContain("Starting a disposable Linux sandbox");
    expect(html).not.toMatch(/\$RC\(|<template id="B:/);
  });
});
