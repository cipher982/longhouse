import { describe, expect, it } from "vitest";
import type { ToolInteraction } from "@/shared/session/model";
import { liveOutputTail } from "../RunningCallRow";

const call = (out: string | null) =>
  ({ callEvent: { tool_output_text: out } }) as unknown as ToolInteraction;

describe("liveOutputTail", () => {
  it("is null with no output so the 'No output yet' note stays", () => {
    expect(liveOutputTail(call(null))).toBeNull();
    expect(liveOutputTail(call("  \n"))).toBeNull();
  });
  it("keeps the last 12 lines", () => {
    const text = Array.from({ length: 30 }, (_, i) => `l${i + 1}`).join("\n");
    const tail = liveOutputTail(call(text))!.split("\n");
    expect(tail).toHaveLength(12);
    expect(tail[11]).toBe("l30");
  });
});
