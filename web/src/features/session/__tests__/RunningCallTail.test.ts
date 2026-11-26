import { describe, expect, it } from "vitest";
import type { ToolInteraction } from "@/shared/session/model";
import { liveOutputTail } from "../RunningCallRow";

const call = (out: string | null) =>
  ({ callEvent: { tool_output_text: out } }) as unknown as ToolInteraction;

describe("liveOutputTail", () => {
  it("is null with no output, so no empty tail is drawn", () => {
    expect(liveOutputTail(call(null))).toBeNull();
    expect(liveOutputTail(call("  \n"))).toBeNull();
  });
  it("keeps the last two non-blank lines", () => {
    const text = Array.from({ length: 30 }, (_, i) => `l${i + 1}`).join("\n") + "\n\n";
    expect(liveOutputTail(call(text))).toBe("l29\nl30");
  });
});
