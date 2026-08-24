import { describe, expect, it } from "vitest";

import { activityEvidenceIsLive, isActivityExecuting, isActivityStalled } from "../activityEvidence";

const AT = (iso: string) => Date.parse(iso);

describe("activityEvidence", () => {
  it("treats evidence inside its window as live", () => {
    const activity = { state: "executing", valid_until: "2026-08-23T12:10:00Z" };
    expect(isActivityExecuting(activity, AT("2026-08-23T12:05:00Z"))).toBe(true);
  });

  it("stops reporting work once the window has passed", () => {
    // The ten-hour wedge: the server is correct, no further frame arrives, and
    // an open tab keeps rendering the last snapshot it saw.
    const activity = { state: "executing", valid_until: "2026-08-23T12:10:00Z" };
    expect(isActivityExecuting(activity, AT("2026-08-23T22:10:00Z"))).toBe(false);
  });

  it("expires stalled evidence too, not just executing", () => {
    const activity = { state: "stalled", valid_until: "2026-08-23T12:10:00Z" };
    expect(isActivityStalled(activity, AT("2026-08-23T12:05:00Z"))).toBe(true);
    expect(isActivityStalled(activity, AT("2026-08-23T12:11:00Z"))).toBe(false);
  });

  it("does not invent an expiry when no window is served", () => {
    const activity = { state: "executing", valid_until: null };
    expect(isActivityExecuting(activity, AT("2030-01-01T00:00:00Z"))).toBe(true);
  });

  it("does not invent an expiry from an unparseable window", () => {
    const activity = { state: "executing", valid_until: "not-a-timestamp" };
    expect(activityEvidenceIsLive(activity, AT("2030-01-01T00:00:00Z"))).toBe(true);
  });

  it("expiry yields unknown, never a manufactured ending", () => {
    // Expired evidence must not read as idle or finished; it reads as nothing.
    const activity = { state: "executing", valid_until: "2026-08-23T12:10:00Z" };
    const later = AT("2026-08-23T13:00:00Z");
    expect(isActivityExecuting(activity, later)).toBe(false);
    expect(isActivityStalled(activity, later)).toBe(false);
  });

  it("treats missing activity as not live", () => {
    expect(activityEvidenceIsLive(null, AT("2026-08-23T12:00:00Z"))).toBe(false);
  });
});
