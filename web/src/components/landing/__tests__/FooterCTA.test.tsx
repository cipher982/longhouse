import { render, screen } from "@testing-library/react";
import { MemoryRouter, matchRoutes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { FooterCTA } from "../FooterCTA";

vi.mock("../../../lib/analytics", () => ({
  trackAcquisitionEvent: vi.fn(),
}));

function renderFooter() {
  return render(
    <MemoryRouter>
      <FooterCTA />
    </MemoryRouter>
  );
}

describe("FooterCTA", () => {
  it("links the legal pages users are told to read", () => {
    renderFooter();

    const nav = screen.getByRole("navigation", { name: "Footer" });
    const hrefs = Array.from(nav.querySelectorAll("a")).map((a) => a.getAttribute("href"));

    // Privacy, Security, and Terms cross-reference each other in their copy; a
    // page reachable only by typing the URL is not actually published.
    expect(hrefs).toContain("/privacy");
    expect(hrefs).toContain("/security");
    expect(hrefs).toContain("/terms");
  });

  it(
    "keeps every internal footer link resolvable in the router",
    async () => {
      const { buildAppRoutes } = await import("../../../routes/App");
      renderFooter();

      const nav = screen.getByRole("navigation", { name: "Footer" });
      const internal = Array.from(nav.querySelectorAll("a"))
        .map((a) => a.getAttribute("href") ?? "")
        .filter((href) => href.startsWith("/"));

      expect(internal.length).toBeGreaterThan(0);

      for (const demoMode of [false, true]) {
        for (const href of internal) {
          const matches = matchRoutes(buildAppRoutes({ demoMode, singleTenant: false }), href);
          const leafPath = matches?.at(-1)?.route.path;

          expect(matches, `Expected ${href} to resolve (demoMode=${demoMode})`).not.toBeNull();
          expect(
            leafPath,
            `Expected ${href} to avoid the wildcard fallback (demoMode=${demoMode})`
          ).not.toBe("*");
        }
      }
    },
    15_000
  );
});
