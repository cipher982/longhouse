import { describe, expect, it } from "vitest";
import { bucketTimestamps } from "../activityBuckets";

const NOW = Date.parse("2026-04-15T16:12:00Z");

describe("bucketTimestamps", () => {
  it("buckets timestamps into fixed windows ending at now, oldest first", () => {
    const buckets = bucketTimestamps(
      ["2026-04-15T15:44:00Z", "2026-04-15T15:44:20Z", "2026-04-15T16:09:00Z"],
      { nowMs: NOW, windowMinutes: 30, bucketMinutes: 1 },
    );
    expect(buckets).toHaveLength(30);
    expect(buckets[2]).toBe(2); // 15:44 is 28 minutes before now -> bucket index 2
    expect(buckets[27]).toBe(1); // 16:09 is 3 minutes before now -> bucket index 27
  });

  it("drops timestamps outside the window, including ones after now", () => {
    const buckets = bucketTimestamps(
      ["2026-04-15T14:00:00Z", "2026-04-15T16:20:00Z", null, undefined],
      { nowMs: NOW, windowMinutes: 30, bucketMinutes: 1 },
    );
    expect(buckets.every((value) => value === 0)).toBe(true);
  });

  it("ignores unparseable timestamps instead of throwing", () => {
    expect(() =>
      bucketTimestamps(["not-a-date"], { nowMs: NOW, windowMinutes: 10, bucketMinutes: 5 }),
    ).not.toThrow();
  });
});
