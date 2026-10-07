import { act, StrictMode } from "react";
import { hydrateRoot } from "react-dom/client";
import { prerender } from "react-dom/static";
import { MemoryRouter } from "react-router";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderRoute } from "../prerender";
import { routeChunkModules } from "../routeChunks";

afterEach(() => {
  delete window.__APP_MODE__;
});

// What main.tsx does on a prerendered page, as a browser runs it: a fresh copy
// of the app (no lazy route resolved yet, the public site's demo config) loads
// the route's lazy chunks, then hydrates the static HTML under StrictMode.
async function hydrate(pathname: string) {
  const { html } = await renderRoute(pathname);
  // React's static renderer leaves its last context values set until its next
  // render starts; the build prerenders in its own process, but here the
  // client renders next in the same realm and would see the StaticRouter's.
  await prerender(<></>);
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  const heading = container.querySelector("h1");

  // renderRoute resolved App's lazy routes in this module graph; the browser
  // starts with none resolved, so hydrate from a fresh one.
  vi.resetModules();
  window.__APP_MODE__ = "demo";
  const { AppContent, AppProviders } = await import("../AppRoot");
  const { loadRouteChunks } = await import("../routeChunks");
  await loadRouteChunks(pathname);

  const recoverable: unknown[] = [];
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  let root: ReturnType<typeof hydrateRoot> | undefined;
  await act(async () => {
    root = hydrateRoot(
      container,
      <StrictMode>
        <AppProviders queryClient={new QueryClient()}>
          <MemoryRouter initialEntries={[pathname]}>
            <AppContent />
          </MemoryRouter>
        </AppProviders>
      </StrictMode>,
      { onRecoverableError: (error) => recoverable.push(error) },
    );
  });
  const errors = consoleError.mock.calls.filter((call) => /hydrat/i.test(String(call[0])));
  consoleError.mockRestore();
  return { container, heading, recoverable, errors, unmount: () => act(() => root?.unmount()) };
}

describe("hydrating prerendered pages", () => {
  it.each(["/", "/docs", "/docs/cli", "/docs/integrations"])(
    "%s adopts its static HTML without a mismatch",
    async (pathname) => {
      const { container, heading, recoverable, errors, unmount } = await hydrate(pathname);

      expect(recoverable).toEqual([]);
      expect(errors).toEqual([]);
      // Hydration adopts the server's nodes; a client re-render would replace them.
      expect(heading).not.toBeNull();
      expect(container.querySelector("h1")).toBe(heading);
      await unmount();
      container.remove();
    },
  );

  it("links the docs chunk into docs pages only", () => {
    const docs = ["src/features/marketing/docs/DocsRoutes.tsx"];
    expect(routeChunkModules("/docs")).toEqual(docs);
    expect(routeChunkModules("/docs/cli")).toEqual(docs);
    expect(routeChunkModules("/")).toEqual([]);
    expect(routeChunkModules("/blog")).toEqual([]);
    expect(routeChunkModules("/docsearch")).toEqual([]);
  });
});
