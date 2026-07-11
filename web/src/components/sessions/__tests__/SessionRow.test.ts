import { describe, expect, it } from "vitest";
import { getRowControlPresentation, getRowTimeLabel } from "../SessionRow";
import { makeSessionStateFacts } from "../../../test/sessionState";

const NOW = Date.parse("2026-05-19T16:00:00Z");

describe("getRowTimeLabel", () => {
  it("uses the server-owned status timestamp when present", () => {
    expect(
      getRowTimeLabel({
        seenAt: "2026-05-19T15:57:00Z",
        seenAtPrefix: "Updated",
        startedAt: "2026-05-19T14:00:00Z",
        relativeNowMs: NOW,
      }),
    ).toBe("Updated 3m ago");
  });

  it("falls back to an explicit started label", () => {
    expect(
      getRowTimeLabel({
        seenAt: null,
        seenAtPrefix: null,
        startedAt: "2026-05-19T15:00:00Z",
        relativeNowMs: NOW,
      }),
    ).toBe("Started 1h ago");
  });
});

describe("getRowControlPresentation", () => {
  it("shows live control when direct input is available", () => {
    expect(
      getRowControlPresentation(makeSessionStateFacts({ access: "live_control" })),
    ).toMatchObject({
      label: "Live control",
      tone: "live",
    });
  });

  it("shows reattach for managed sessions that are not live right now", () => {
    expect(
      getRowControlPresentation(makeSessionStateFacts({ access: "reattach" })),
    ).toMatchObject({
      label: "Reattach",
      tone: "reattach",
    });
  });

  it("distinguishes observe-only transcript tails from imported search-only sessions", () => {
    expect(
      getRowControlPresentation(makeSessionStateFacts({ access: "observe_only" })),
    ).toMatchObject({
      label: "Observe only",
      tone: "observe",
    });
  });

  it("falls back to search-only for imported or missing capability payloads", () => {
    expect(
      getRowControlPresentation(makeSessionStateFacts({ access: "search_only" })),
    ).toMatchObject({
      label: "Search only",
      tone: "search",
    });
    expect(getRowControlPresentation(makeSessionStateFacts({ access: null }))).toMatchObject({
      label: "Search only",
      tone: "search",
    });
  });

  it("treats the server state presentation as canonical", () => {
    expect(
      getRowControlPresentation(makeSessionStateFacts({ access: "reattach" })),
    ).toMatchObject({
      label: "Reattach",
      tone: "reattach",
    });
  });
});
