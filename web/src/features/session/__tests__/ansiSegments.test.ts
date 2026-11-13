import { describe, expect, it } from "vitest";
import { hasAnsiSequences, parseAnsiSegments, stripAnsi } from "../ansiSegments";

describe("ansiSegments", () => {
  it("parses standard SGR colors, bold, dim, and reset", () => {
    const input = "\x1b[32mPASS\x1b[0m \x1b[1;31mFAIL\x1b[22m norm\x1b[0m";
    expect(hasAnsiSequences(input)).toBe(true);
    expect(parseAnsiSegments(input)).toEqual([
      { text: "PASS", className: "tl-ansi-green" },
      { text: " ", className: null },
      { text: "FAIL", className: "tl-ansi-red tl-ansi-bold" },
      { text: " norm", className: "tl-ansi-red" },
    ]);
  });

  it("handles 256-color and truecolor sequences without leaking parameters", () => {
    const input = "\x1b[38;5;10mok\x1b[38;2;255;100;0m warm\x1b[0m";
    expect(parseAnsiSegments(input)).toEqual([
      { text: "ok warm", className: "tl-ansi-green" },
    ]);
  });

  it("strips non-SGR CSI and OSC sequences cleanly", () => {
    const input = "\x1b[2K\x1b]0;title\x07\x1b[36mready\x1b[0m";
    expect(parseAnsiSegments(input)).toEqual([
      { text: "ready", className: "tl-ansi-cyan" },
    ]);
    expect(stripAnsi(input)).toBe("ready");
  });

  it("caps over 400 lines by stripping ANSI and returning a single plain segment", () => {
    const huge = Array.from({ length: 405 }, (_, i) => `\x1b[32mline ${i}\x1b[0m`).join("\n");
    const segs = parseAnsiSegments(huge);
    expect(segs).toHaveLength(1);
    expect(segs[0].className).toBeNull();
    expect(segs[0].text).not.toContain("\x1b");
  });
});
