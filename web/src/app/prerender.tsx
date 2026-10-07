import { QueryClient } from "@tanstack/react-query";
import { StaticRouter } from "react-router";
import { prerender } from "react-dom/static";
import config from "@/shared/lib/config";
import { PageMetaCollectorContext, type CollectedPageMeta } from "@/shared/hooks/usePageMeta";
import { AppContent, AppProviders } from "./AppRoot";
export { routeChunkModules } from "./routeChunks";

// Entry for web/scripts/prerender.mjs, loaded by Vite in Node at build time.
//
// The prerendered pages are the public site's (longhouse.ai runs the Runtime
// Host in demo mode, and the server only hands these pages out in that mode),
// so render with the demo site's config: what the browser will hydrate.
Object.assign(config, { appMode: "demo", demoMode: true, authEnabled: true, singleTenant: false });

export interface PrerenderedRoute {
  html: string;
  meta: CollectedPageMeta;
}

/** Render one route of the real app to HTML, waiting for lazy sections too. */
export async function renderRoute(pathname: string): Promise<PrerenderedRoute> {
  const meta: CollectedPageMeta = {};
  const errors: unknown[] = [];
  const { prelude } = await prerender(
    <PageMetaCollectorContext.Provider value={meta}>
      <AppProviders queryClient={new QueryClient()}>
        <StaticRouter location={pathname}>
          <AppContent />
        </StaticRouter>
      </AppProviders>
    </PageMetaCollectorContext.Provider>,
    {
      // Boundaries bigger than the default 12.8 KB are emitted out of order as
      // hidden segments plus inline $RC scripts, which the CSP blocks and
      // crawlers read as hidden text. Inline every finished boundary in place.
      progressiveChunkSize: Number.MAX_SAFE_INTEGER,
      onError: (error) => {
        errors.push(error);
      },
    },
  );
  const html = await new Response(prelude).text();
  // A boundary that failed here is client-rendered later: the static HTML would
  // be missing that section and hydration would report a mismatch.
  if (errors.length > 0) {
    throw new Error(`prerender ${pathname}: ${errors.map((error) => String(error)).join("; ")}`);
  }
  // Streaming instructions mean a boundary was not inlined; the CSP blocks the
  // inline script that would complete it, so the section would never appear.
  if (/<template id="B:|\$RC\(/.test(html)) {
    throw new Error(`prerender ${pathname}: a Suspense boundary was emitted out of order`);
  }
  return { html, meta };
}
