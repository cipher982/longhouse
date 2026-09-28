import { describe, expect, it } from "vitest";
import { childrenForToolCall, runIdsInToolOutput, subagentLabel, summarizeSubagents } from "../subagents";
import type { SubagentChild } from "../types";

function child(overrides: Partial<SubagentChild> = {}): SubagentChild {
  return {
    session_id: "child-1",
    provider: "claude",
    parent_tool_call_id: null,
    run_id: null,
    started_at: "2026-08-25T03:30:00Z",
    ended_at: "2026-08-25T03:31:00Z",
    user_messages: 1,
    assistant_messages: 3,
    tool_calls: 12,
    title: null,
    first_user_message_preview: null,
    last_visible_text_preview: null,
    ...overrides,
  };
}

const WORKFLOW_RESULT =
  "Workflow launched in background. Task ID: wkjlcxlhn\n" +
  "Transcript dir: /Users/d/.claude/projects/-p/02b4c48f/subagents/workflows/wf_4f413c80-395\n" +
  "Script file: /Users/d/.claude/projects/-p/workflows/scripts/cp-wf_4f413c80-395.js";

describe("runIdsInToolOutput", () => {
  it("reads the run id the parent's own tool result names", () => {
    expect(runIdsInToolOutput(WORKFLOW_RESULT)).toEqual(["wf_4f413c80-395"]);
  });

  it("finds nothing in output that never names a transcript dir", () => {
    expect(runIdsInToolOutput("Workflow launched in background. Task ID: wkjlcxlhn")).toEqual([]);
    expect(runIdsInToolOutput(null)).toEqual([]);
  });

  it("does not carry state between calls", () => {
    expect(runIdsInToolOutput(WORKFLOW_RESULT)).toEqual(["wf_4f413c80-395"]);
    expect(runIdsInToolOutput(WORKFLOW_RESULT)).toEqual(["wf_4f413c80-395"]);
  });
});

describe("childrenForToolCall", () => {
  it("binds a Task child by the tool call its sidecar named", () => {
    const task = child({ session_id: "task", parent_tool_call_id: "toolu_a" });
    expect(childrenForToolCall([task], { toolCallId: "toolu_a", toolOutputText: null })).toEqual([task]);
  });

  it("binds a whole workflow run through the parent's tool result", () => {
    const workers = [
      child({ session_id: "w1", run_id: "wf_4f413c80-395", started_at: "2026-08-25T03:30:00Z" }),
      child({ session_id: "w2", run_id: "wf_4f413c80-395", started_at: "2026-08-25T03:29:00Z" }),
    ];
    const bound = childrenForToolCall(workers, { toolCallId: "toolu_wf", toolOutputText: WORKFLOW_RESULT });
    expect(bound.map((entry) => entry.session_id)).toEqual(["w2", "w1"]);
  });

  it("fails closed: an unnamed run binds to nothing", () => {
    const orphan = child({ session_id: "w1", run_id: "wf_other" });
    expect(childrenForToolCall([orphan], { toolCallId: "toolu_wf", toolOutputText: WORKFLOW_RESULT })).toEqual([]);
    expect(childrenForToolCall([orphan], { toolCallId: null, toolOutputText: null })).toEqual([]);
  });

  it("never binds by proximity — a different call gets none of them", () => {
    const worker = child({ session_id: "w1", run_id: "wf_4f413c80-395" });
    expect(childrenForToolCall([worker], { toolCallId: "toolu_unrelated", toolOutputText: "ran a command" })).toEqual([]);
  });
});

describe("summarizeSubagents", () => {
  it("states the shape of the work before anyone expands it", () => {
    const workers = [
      child({ started_at: "2026-08-25T03:30:00Z", ended_at: "2026-08-25T03:34:12Z" }),
      child({ session_id: "w2", started_at: "2026-08-25T03:30:05Z", ended_at: "2026-08-25T03:32:00Z" }),
    ];
    expect(summarizeSubagents(workers)).toBe("2 agents · 4m12s");
  });

  it("drops the duration when a worker has not finished", () => {
    expect(summarizeSubagents([child({ ended_at: null })])).toBe("1 agent");
  });
});

describe("subagentLabel", () => {
  it("prefers a title, falls back to the prompt it was handed", () => {
    expect(subagentLabel(child({ title: "Harden the container" }))).toBe("Harden the container");
    expect(subagentLabel(child({ first_user_message_preview: "You are one of ~22 agents" }))).toBe(
      "You are one of ~22 agents",
    );
    expect(subagentLabel(child({ session_id: "abcdef1234" }))).toBe("abcdef12");
  });
});
