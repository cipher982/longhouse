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

  it("includes loop in the authenticated app navigation", async () => {
    const { getNavItems } = await import("./navItems");
    expect(getNavItems(null)).toEqual([
      { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
      { label: "Loop", href: "/loop", testId: "global-loop-tab" },
      { label: "Oikos", href: "/chat", testId: "global-chat-tab" },
    ]);
  });

  it("preserves admin navigation", async () => {
    const { getNavItems } = await import("./navItems");
    expect(getNavItems("ADMIN").map((item) => item.href)).toEqual([
      "/timeline",
      "/loop",
      "/chat",
      "/admin",
    ]);
  });

  it("keeps demo navigation minimal", async () => {
    configState.demoMode = true;
    const { getNavItems } = await import("./navItems");
    expect(getNavItems("ADMIN")).toEqual([
      { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
    ]);
  });
});
