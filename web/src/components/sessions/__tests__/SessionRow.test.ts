import { describe, expect, it } from "vitest";
import { getRowTimeLabel } from "../SessionRow";

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
