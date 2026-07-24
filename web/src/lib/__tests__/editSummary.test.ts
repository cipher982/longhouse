/**
 * Edit-shape detection and diff stats (`docs/specs/transcript-action-visibility.md`).
 */
import { describe, expect, it, vi } from "vitest";
import { DIFF_CELL_BUDGET, formatEditStat, getEditStat } from "../sessionWorkspace";
import type { ToolInteraction } from "../sessionWorkspace";
import type { AgentEvent } from "../../services/api/agents";

function editInteraction(input: Record<string, unknown> | null): ToolInteraction {
  return {
    key: `edit-${Math.random()}`,
    toolName: "Edit",
    callEvent: {
      id: 1,
      role: "assistant",
      tool_name: "Edit",
      tool_input_json: input,
      timestamp: "2026-01-01T00:00:00Z",
    } as AgentEvent,
    resultEvent: { id: 2, role: "tool", timestamp: "2026-01-01T00:00:01Z" } as AgentEvent,
    pairing: "id",
    anchorId: 1,
    timestamp: "2026-01-01T00:00:00Z",
  };
}

describe("getEditStat", () => {
  it("counts a replace via line diff and shows only the basename", () => {
    const stat = getEditStat(
      editInteraction({ file_path: "a/b/c/timelineModel.ts", old_string: "a\nb\nc", new_string: "a\nB\nc" }),
    );
    expect(stat.added).toBe(1);
    expect(stat.removed).toBe(1);
    expect(stat.filePath).toBe("a/b/c/timelineModel.ts");
    expect(formatEditStat(stat)).toBe("timelineModel.ts +1 −1");
  });

  it("treats a write as all-added and a lone old_string as all-removed", () => {
    expect(formatEditStat(getEditStat(editInteraction({ file_path: "n.ts", content: "1\n2\n3" }))))
      .toBe("n.ts +3 −0");
    expect(formatEditStat(getEditStat(editInteraction({ file_path: "d.ts", old_string: "1\n2" }))))
      .toBe("d.ts +0 −2");
  });

  it("counts apply_patch hunks and ignores file headers", () => {
    const stat = getEditStat(
      editInteraction({ file_path: "p.ts", patch: "--- a/p.ts\n+++ b/p.ts\n@@\n-old\n+new\n+extra" }),
    );
    expect(stat.added).toBe(2);
    expect(stat.removed).toBe(1);
  });

  it("names the file but never fabricates a stat for an unknown shape", () => {
    const stat = getEditStat(editInteraction({ file_path: "mystery.ts", mode: "rewrite" }));
    expect(stat.hasStat).toBe(false);
    expect(formatEditStat(stat)).toBe("mystery.ts");
  });

  it("returns nothing renderable when there is no recoverable input", () => {
    expect(formatEditStat(getEditStat(editInteraction(null)))).toBeNull();
    expect(formatEditStat(getEditStat(editInteraction({})))).toBeNull();
  });

  it("skips the LCS entirely when the input exceeds the cell budget", () => {
    // Guard must fire *before* the quadratic table is allocated, so a diff this
    // large has to be cheap rather than merely un-rendered.
    const lines = Math.ceil(Math.sqrt(DIFF_CELL_BUDGET)) + 10;
    const oldStr = Array.from({ length: lines }, (_, i) => `old ${i}`).join("\n");
    const newStr = Array.from({ length: lines }, (_, i) => `new ${i}`).join("\n");

    const started = performance.now();
    const stat = getEditStat(editInteraction({ file_path: "huge.ts", old_string: oldStr, new_string: newStr }));
    const elapsed = performance.now() - started;

    expect(stat.hasStat).toBe(false);
    expect(formatEditStat(stat)).toBe("huge.ts");
    expect(elapsed).toBeLessThan(250);
  });

  it("memoizes per interaction so render paths do not re-diff", () => {
    const interaction = editInteraction({ file_path: "m.ts", old_string: "a", new_string: "b" });
    const first = getEditStat(interaction);
    const spy = vi.spyOn(JSON, "stringify");
    expect(getEditStat(interaction)).toBe(first);
    spy.mockRestore();
  });
});
