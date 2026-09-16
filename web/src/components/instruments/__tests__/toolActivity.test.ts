import { describe, expect, it } from "vitest";
import type { TimelineItem, ToolInteraction } from "../../../lib/sessionWorkspace";
import { countToolCallsThisTurn, findRunningTool, getRunningTurnStartMs } from "../toolActivity";
import { formatElapsedClock, getSessionHeaderState } from "../../session-workspace/sessionHeaderState";
import type { AgentSession } from "../../../services/api/agents";

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

describe("getRunningTurnStartMs", () => {
  it("returns the most recent user message's timestamp", () => {
    const items: TimelineItem[] = [
      userMessage(1),
      toolItem(toolInteraction({ key: "a" })),
      {
        kind: "message",
        event: {
          id: 2,
          role: "user",
          content_text: "go again",
          tool_name: null,
          tool_input_json: null,
          tool_output_text: null,
          tool_call_id: null,
          timestamp: "2026-04-15T16:05:00Z",
          in_active_context: true,
        },
      },
      toolItem(toolInteraction({ key: "b" })),
    ];
    expect(getRunningTurnStartMs(items)).toBe(Date.parse("2026-04-15T16:05:00Z"));
  });

  it("returns null when no user message has loaded", () => {
    const items: TimelineItem[] = [toolItem(toolInteraction({ key: "a" }))];
    expect(getRunningTurnStartMs(items)).toBeNull();
  });
});

/**
 * The bug this closes: the session header, the composer clock, and the
 * readout rail's Turn readout each derived elapsed time from
 * `activity.observed_at` independently — a timestamp that moves every time a
 * new tool call starts, so a turn with 53 tool calls still read close to
 * "0:00" on all three. This feeds one fixture through the one shared anchor
 * (`getRunningTurnStartMs`) and asserts the header text, the composer clock,
 * and the rail's raw seconds all agree with each other.
 */
describe("turn-elapsed agreement across header, composer, and rail", () => {
  it("derives the same non-zero elapsed time everywhere from one turn start", () => {
    const items: TimelineItem[] = [
      userMessage(1), // timestamp fixed at 2026-04-15T15:59:00Z above
      toolItem(toolInteraction({ key: "a" })),
      toolItem(toolInteraction({ key: "b" })),
    ];
    const nowMs = Date.parse("2026-04-15T16:12:00Z");
    const turnStartMs = getRunningTurnStartMs(items);
    expect(turnStartMs).toBe(Date.parse("2026-04-15T15:59:00Z"));

    const session: Pick<AgentSession, "session_state"> = {
      session_state: {
        disposition: { state: "open" },
        pending_interaction: null,
        activity: {
          state: "executing",
          tool: "hub",
          // Deliberately recent — near `nowMs` — so a fix that still reads
          // this field for elapsed time would report ~0 instead of ~13m.
          observed_at: "2026-04-15T16:11:55Z",
        },
        presentation: {
          primary: { tone: "running", label: "Running" },
        },
        last_result_at: null,
      } as never,
    };

    // The rail's own formula (SessionDetailPage.tsx turnElapsedSeconds).
    const railSeconds = Math.max(0, Math.floor((nowMs - turnStartMs!) / 1_000));
    // The composer's own formula (SessionChat.tsx composerElapsedSeconds).
    const composerSeconds = Math.max(0, Math.floor((nowMs - turnStartMs!) / 1_000));
    // The header's own formula, driven through the same turnStartMs.
    const headerState = getSessionHeaderState(session, nowMs, turnStartMs);

    expect(railSeconds).toBe(composerSeconds);
    expect(railSeconds).toBe(13 * 60);
    expect(headerState.text).toBe("Using hub for 13 minutes");
    // Same clock format the composer/rail render this many seconds as.
    expect(formatElapsedClock(railSeconds)).toBe("13:00");
  });
});

describe("findRunningTool", () => {
  it("returns null when nothing is running", () => {
    const items: TimelineItem[] = [toolItem(toolInteraction())];
    expect(findRunningTool(items)).toBeNull();
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
    expect(findRunningTool(items)).toEqual({ toolName: "exec_command", label: "exec_command" });
  });
});
