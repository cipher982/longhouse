import { describe, expect, it } from "vitest";
import type { TimelineItem, ToolInteraction } from "../../../lib/sessionWorkspace";
import { countToolCallsThisTurn, findRunningToolLabel } from "../toolActivity";

function toolInteraction(overrides: Partial<ToolInteraction> = {}): ToolInteraction {
  return {
    key: "tool-1",
    toolName: "Bash",
    callEvent: null,
    resultEvent: {
      id: 2,
      role: "tool",
      content_text: null,
      tool_name: "Bash",
      tool_input_json: null,
      tool_output_text: "ok",
      tool_call_id: "c1",
      timestamp: "2026-04-15T16:00:00Z",
      in_active_context: true,
    },
    pairing: "id",
    anchorId: 1,
    timestamp: "2026-04-15T16:00:00Z",
    presentation: null,
    // Spread last so an explicit `null` override (e.g. resultEvent: null for
    // a still-running interaction) isn't swallowed by a `??` default.
    ...overrides,
  };
}

function userMessage(id: number): TimelineItem {
  return {
    kind: "message",
    event: {
      id,
      role: "user",
      content_text: "go",
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp: "2026-04-15T15:59:00Z",
      in_active_context: true,
    },
  };
}

function toolItem(interaction: ToolInteraction): TimelineItem {
  return { kind: "tool", interaction };
}

describe("countToolCallsThisTurn", () => {
  it("counts tool calls only since the last user message", () => {
    const items: TimelineItem[] = [
      toolItem(toolInteraction({ key: "before-1" })),
      userMessage(1),
      toolItem(toolInteraction({ key: "after-1" })),
      toolItem(toolInteraction({ key: "after-2" })),
    ];
    expect(countToolCallsThisTurn(items)).toBe(2);
  });

  it("counts every loaded tool call when no user message has loaded yet", () => {
    const items: TimelineItem[] = [
      toolItem(toolInteraction({ key: "a" })),
      toolItem(toolInteraction({ key: "b" })),
    ];
    expect(countToolCallsThisTurn(items)).toBe(2);
  });
});

describe("findRunningToolLabel", () => {
  it("returns null when nothing is running", () => {
    const items: TimelineItem[] = [toolItem(toolInteraction())];
    expect(findRunningToolLabel(items)).toBeNull();
  });

  it("returns the running tool's own toolName when it has no callEvent input to summarize", () => {
    const items: TimelineItem[] = [
      toolItem(
        toolInteraction({
          key: "running",
          toolName: "exec_command",
          resultEvent: null,
          callEvent: {
            id: 1,
            role: "assistant",
            content_text: null,
            tool_name: "exec_command",
            tool_input_json: null,
            tool_output_text: null,
            tool_call_id: "c1",
            tool_call_state: "running",
            timestamp: "2026-04-15T16:11:00Z",
            in_active_context: true,
          },
        }),
      ),
    ];
    expect(findRunningToolLabel(items)).toBe("exec_command");
  });
});
