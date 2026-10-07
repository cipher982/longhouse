import { act } from "react";
import { hydrateRoot } from "react-dom/client";
import { prerender } from "react-dom/static";
import { MemoryRouter } from "react-router";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { AppContent, AppProviders } from "../AppRoot";
// Importing the prerender entry puts config in demo mode, as on the public site.
import { renderRoute, routeChunkModules } from "../prerender";
import { DOCS_ROUTES_MODULE, isDocsPath, loadDocsRoutes } from "../routeChunks";

// What main.tsx does on a prerendered page: the same tree, hydrated onto the
// static HTML, after loading the lazy docs chunk on a docs route.
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
  if (isDocsPath(pathname)) await loadDocsRoutes();

  const recoverable: unknown[] = [];
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  let root: ReturnType<typeof hydrateRoot> | undefined;
  await act(async () => {
    root = hydrateRoot(
      container,
      <AppProviders queryClient={new QueryClient()}>
        <MemoryRouter initialEntries={[pathname]}>
          <AppContent />
        </MemoryRouter>
      </AppProviders>,
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
    expect(routeChunkModules("/docs")).toEqual([DOCS_ROUTES_MODULE]);
    expect(routeChunkModules("/docs/cli")).toEqual([DOCS_ROUTES_MODULE]);
    expect(routeChunkModules("/")).toEqual([]);
    expect(routeChunkModules("/blog")).toEqual([]);
    expect(routeChunkModules("/docsearch")).toEqual([]);
  });
});
