import { describe, expect, it } from "vitest";
import { getSessionStartedLabel } from "../sessionTiming";

const NOW = Date.parse("2026-03-22T22:04:30Z");

describe("getSessionStartedLabel", () => {
  it("stamps session age coarsely rather than counting seconds", () => {
    expect(
      getSessionStartedLabel({ started_at: "2026-03-22T12:00:00Z" }, NOW),
    ).toBe("Started 10h ago");
    expect(
      getSessionStartedLabel({ started_at: "2026-03-22T22:00:00Z" }, NOW),
    ).toBe("Started 4m ago");
  });

  it("reads as a sentence for a session that just started", () => {
    expect(
      getSessionStartedLabel({ started_at: "2026-03-22T22:04:20Z" }, NOW),
    ).toBe("Started just now");
  });

  it("says nothing when the session has no start time", () => {
    expect(getSessionStartedLabel(null, NOW)).toBeNull();
    expect(getSessionStartedLabel({ started_at: "" }, NOW)).toBeNull();
  });
});
