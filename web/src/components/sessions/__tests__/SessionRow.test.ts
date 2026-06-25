import { describe, expect, it } from "vitest";
import { getRowControlPresentation, getRowTimeLabel } from "../SessionRow";

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
      getRowControlPresentation({
        live_control_available: true,
        host_reattach_available: true,
        reply_to_live_session_available: true,
      }),
    ).toMatchObject({
      label: "Live control",
      tone: "live",
    });
  });

  it("shows reattach for managed sessions that are not live right now", () => {
    expect(
      getRowControlPresentation({
        live_control_available: false,
        host_reattach_available: true,
        reply_to_live_session_available: false,
        control_label: "reattach",
      }),
    ).toMatchObject({
      label: "Reattach",
      tone: "reattach",
    });
  });

  it("distinguishes observe-only transcript tails from imported search-only sessions", () => {
    expect(
      getRowControlPresentation({
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
        observe_only: true,
        control_label: "search-only",
      }),
    ).toMatchObject({
      label: "Observe only",
      tone: "observe",
    });
  });

  it("falls back to search-only for imported or missing capability payloads", () => {
    expect(
      getRowControlPresentation({
        live_control_available: false,
        host_reattach_available: false,
        reply_to_live_session_available: false,
        search_only: true,
        control_label: "imported",
      }),
    ).toMatchObject({
      label: "Search only",
      tone: "search",
    });
    expect(getRowControlPresentation(null)).toMatchObject({
      label: "Search only",
      tone: "search",
    });
  });
});
