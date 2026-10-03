import { matchRoutes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const configState = {
  demoMode: false,
  singleTenant: false,
};

vi.mock("@/shared/lib/config", () => ({
  default: configState,
}));

describe("getNavItems", () => {
  beforeEach(() => {
    configState.demoMode = false;
    configState.singleTenant = false;
  });

  it("includes core items in the authenticated app navigation", async () => {
    const { getNavItems } = await import("./navItems");
    expect(getNavItems()).toEqual([
      { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
      { label: "Machines", href: "/machines", testId: "global-machines-tab" },
    ]);
  });

  it("has no separate Health tab in single-tenant mode", async () => {
    configState.singleTenant = true;
    const { getNavItems } = await import("./navItems");
    expect(getNavItems().map((item) => item.href)).toEqual([
      "/timeline",
      "/machines",
    ]);
  });

  it(
    "keeps top-level nav items aligned to real app routes",
    async () => {
      const { getNavItems } = await import("./navItems");
      const { buildAppRoutes } = await import("../App");

      for (const item of getNavItems()) {
        const matches = matchRoutes(
          buildAppRoutes({ demoMode: false, singleTenant: true }),
          item.href,
        );
        const leafPath = matches?.at(-1)?.route.path;

        expect(
          matches,
          `Expected ${item.href} to resolve in the router`,
        ).not.toBeNull();
        expect(
          leafPath,
          `Expected ${item.href} to avoid the wildcard fallback`,
        ).not.toBe("*");
      }
    },
    15_000,
  );

  it("keeps demo navigation minimal", async () => {
    configState.demoMode = true;
    const { getNavItems } = await import("./navItems");
    expect(getNavItems()).toEqual([
      { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
    ]);
  });
});
