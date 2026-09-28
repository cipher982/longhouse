import { describe, expect, it } from "vitest";
import { collapseUnchanged, lineDiff } from "../diff";

describe("lineDiff", () => {
  it("empty -> empty renders nothing", () => {
    expect(lineDiff("", "")).toEqual([]);
  });

  it("empty -> non-empty is all adds", () => {
    const d = lineDiff("", "a\nb");
    expect(d.map((l) => l.kind)).toEqual(["add", "add"]);
    expect(d.map((l) => l.text)).toEqual(["a", "b"]);
  });

  it("non-empty -> empty is all removes", () => {
    const d = lineDiff("a\nb", "");
    expect(d.map((l) => l.kind)).toEqual(["remove", "remove"]);
  });

  it("identical strings are all equal", () => {
    const d = lineDiff("a\nb", "a\nb");
    expect(d.every((l) => l.kind === "equal")).toBe(true);
  });

  it("trailing newline is treated as an empty final line", () => {
    // "a\n" vs "a\n" — split("\n") gives ["a", ""] on both sides.
    const d = lineDiff("a\n", "a\n");
    expect(d.map((l) => l.text)).toEqual(["a", ""]);
    expect(d.every((l) => l.kind === "equal")).toBe(true);
  });

  it("detects a single-line change in the middle", () => {
    const d = lineDiff("a\nb\nc", "a\nB\nc");
    const kinds = d.map((l) => l.kind);
    expect(kinds).toContain("remove");
    expect(kinds).toContain("add");
    expect(kinds[0]).toBe("equal");
    expect(kinds[kinds.length - 1]).toBe("equal");
  });
});

describe("collapseUnchanged", () => {
  it("folds long unchanged runs outside the context window", () => {
    const d = lineDiff(
      ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"].join("\n"),
      ["a", "b", "c", "d", "E", "f", "g", "h", "i", "j"].join("\n"),
    );
    const collapsed = collapseUnchanged(d, 2);
    const markers = collapsed.filter((l) => l.text.startsWith("…"));
    expect(markers.length).toBeGreaterThan(0);
    // The changed lines are always present.
    expect(collapsed.some((l) => l.kind === "remove")).toBe(true);
    expect(collapsed.some((l) => l.kind === "add")).toBe(true);
  });
});
