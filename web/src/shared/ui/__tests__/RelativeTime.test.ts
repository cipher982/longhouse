import { describe, expect, it } from "vitest";
import { formatAgo } from "../RelativeTime";

describe("formatAgo", () => {
  const now = new Date(2026, 10, 26, 15, 0, 0).getTime();
  const at = (y: number, mo: number, d: number, h: number, mi: number) => new Date(y, mo, d, h, mi);

  it("says just now under a minute", () => {
    expect(formatAgo(new Date(now - 20_000), now)).toBe("just now");
  });
  it("counts minutes, then hours", () => {
    expect(formatAgo(new Date(now - 12 * 60_000), now)).toBe("12 min ago");
    expect(formatAgo(new Date(now - 3 * 3_600_000), now)).toBe("3 h ago");
  });
  it("switches to a clock time once it is more than a day old", () => {
    expect(formatAgo(at(2026, 10, 25, 9, 5), now)).toMatch(/^Yesterday /);
    expect(formatAgo(at(2026, 10, 20, 9, 5), now)).toMatch(/Nov 20, /);
    expect(formatAgo(at(2025, 10, 20, 9, 5), now)).toMatch(/2025/);
  });
});
