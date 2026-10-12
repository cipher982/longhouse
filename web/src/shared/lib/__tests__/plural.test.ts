import { describe, expect, it } from "vitest";
import { plural } from "../plural";

describe("plural", () => {
  it("keeps the noun singular at exactly one", () => {
    expect(plural(1, "result")).toBe("1 result");
    expect(plural(1, "matching session")).toBe("1 matching session");
  });

  it("pluralizes every other count, zero included", () => {
    expect(plural(0, "result")).toBe("0 results");
    expect(plural(2, "task thread")).toBe("2 task threads");
  });
});
