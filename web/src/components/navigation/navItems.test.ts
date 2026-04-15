import { matchRoutes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const configState = {
  demoMode: false,
};

vi.mock("../../lib/config", () => ({
  default: configState,
}));

describe("getNavItems", () => {
  beforeEach(() => {
    configState.demoMode = false;
  });

  it("includes core items in the authenticated app navigation", async () => {
    const { getNavItems } = await import("./navItems");
    expect(getNavItems(null)).toEqual([
      { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
    ]);
  });

  it("preserves admin navigation", async () => {
    const { getNavItems } = await import("./navItems");
    expect(getNavItems("ADMIN").map((item) => item.href)).toEqual([
      "/timeline",
      "/admin",
    ]);
  });

  it("keeps top-level nav items aligned to real app routes", async () => {
    const { getNavItems } = await import("./navItems");
    const { buildAppRoutes } = await import("../../routes/App");

    for (const item of getNavItems("ADMIN")) {
      const matches = matchRoutes(buildAppRoutes({ demoMode: false, singleTenant: true }), item.href);
      const leafPath = matches?.at(-1)?.route.path;

      expect(matches, `Expected ${item.href} to resolve in the router`).not.toBeNull();
      expect(leafPath, `Expected ${item.href} to avoid the wildcard fallback`).not.toBe("*");
    }
  });

  it("keeps demo navigation minimal", async () => {
    configState.demoMode = true;
    const { getNavItems } = await import("./navItems");
    expect(getNavItems("ADMIN")).toEqual([
      { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
    ]);
  });
});
