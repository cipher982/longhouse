/**
 * Exploration-run projection helpers — membership, summary copy, overflow.
 */
import { describe, expect, it } from "vitest";
import {
  buildTimelineModel,
  EXPLORATION_OVERFLOW_VISIBLE,
  formatExplorationSummary,
  getToolSummary,
  isExplorationEligible,
  splitExplorationOverflow,
  type ToolInteraction,
} from "../sessionWorkspace";
import type { AgentEvent, AgentSessionProjectionItem } from "../../services/api/agents";

function event(partial: Partial<AgentEvent> & Pick<AgentEvent, "id" | "role" | "timestamp">): AgentEvent {
  return {
    content_text: null,
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    in_active_context: true,
    is_head_branch: true,
    ...partial,
  } as AgentEvent;
}

function projection(events: AgentEvent[]): AgentSessionProjectionItem[] {
  return events.map((evt) => ({
    kind: "event" as const,
    session_id: "s1",
    timestamp: evt.timestamp,
    event: evt,
  }));
}

function interaction(partial: Partial<ToolInteraction> & Pick<ToolInteraction, "toolName" | "key" | "anchorId">): ToolInteraction {
  return {
    callEvent: null,
    resultEvent: { id: 99, role: "tool", timestamp: "2026-01-01T00:00:00Z" } as AgentEvent,
    pairing: "id",
    timestamp: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

describe("exploration run helpers", () => {
  it("summarizes projected patches by files instead of wrapper syntax", () => {
    expect(getToolSummary(interaction({
      toolName: "apply_patch",
      key: "patch",
      anchorId: 1,
      presentation: {
        version: 1,
        disposition: "parsed",
        tool_name: "apply_patch",
        source_tool_name: "exec",
        execution_method: "exec",
        label: "Edited",
        icon: "E",
        color: "brand",
        tier: "action",
        aggregate: null,
        mcp_namespace: null,
        tool_input_json: { patch: "*** Begin Patch\n*** Update File: server/zerg/services/app.py\n*** Add File: server/tests/test_app.py\n*** End Patch" },
        rule_id: "codex:exec:single-child:v1",
        wrapper_recedes: true,
        children: [],
      },
    }))).toBe("app.py + 1 file");
  });

  it("formats semantic verb counts in fixed order", () => {
    expect(
      formatExplorationSummary([
        interaction({ key: "1", anchorId: 1, toolName: "Glob" }),
        interaction({ key: "2", anchorId: 2, toolName: "Read" }),
        interaction({ key: "3", anchorId: 3, toolName: "Grep" }),
        interaction({ key: "4", anchorId: 4, toolName: "Grep" }),
        interaction({ key: "5", anchorId: 5, toolName: "LS" }),
      ]),
    ).toBe("Searched 2 · Read 1 · Listed 2");
  });

  it("splits overflow keeping the latest visible window", () => {
    const items = Array.from({ length: EXPLORATION_OVERFLOW_VISIBLE + 3 }, (_, i) => i);
    const { earlier, latest } = splitExplorationOverflow(items);
    expect(earlier).toEqual([0, 1, 2]);
    expect(latest).toHaveLength(EXPLORATION_OVERFLOW_VISIBLE);
    expect(latest[0]).toBe(3);
  });

  it("marks WebFetch and pending calls ineligible", () => {
    expect(
      isExplorationEligible(
        interaction({
          key: "w",
          anchorId: 1,
          toolName: "WebFetch",
        }),
      ),
    ).toBe(false);
    expect(
      isExplorationEligible(
        interaction({
          key: "p",
          anchorId: 2,
          toolName: "Grep",
          pairing: "pending",
          resultEvent: null,
        }),
      ),
    ).toBe(false);
  });
});

describe("exploration run integration", () => {
  it("replaces Codex polling wrappers with one concise wait group", () => {
    const waitPresentation = {
      version: 1,
      disposition: "parsed" as const,
      tool_name: "write_stdin",
      source_tool_name: "exec",
      execution_method: "exec",
      label: "Wait",
      icon: "…",
      color: "tertiary",
      tier: "noise" as const,
      aggregate: "wait" as const,
      mcp_namespace: null,
      tool_input_json: { session_id: 42, chars: "" },
      rule_id: "codex:exec:single-child:v1",
      wrapper_recedes: true,
      children: [],
    };
    const events: AgentEvent[] = [];
    for (let index = 0; index < 6; index += 1) {
      const callId = `wait-${index}`;
      events.push(
        event({
          id: index * 2 + 1,
          role: "assistant",
          tool_name: "exec",
          tool_call_id: callId,
          tool_input_json: "const r=await tools.write_stdin(...); text(r);",
          tool_presentation: waitPresentation,
          timestamp: `2026-01-01T00:00:${String(index * 2).padStart(2, "0")}Z`,
        }),
        event({
          id: index * 2 + 2,
          role: "tool",
          tool_call_id: callId,
          tool_output_text: "still running",
          timestamp: `2026-01-01T00:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
        }),
      );
    }

    const model = buildTimelineModel(projection(events));

    expect(model.items).toHaveLength(1);
    expect(model.items[0].kind).toBe("noise_group");
    expect(formatExplorationSummary(model.noiseGroups[0].interactions)).toBe("Waited 6");

    const failed = model.noiseGroups[0].interactions[0];
    failed.resultEvent = event({
      id: 99,
      role: "tool",
      tool_output_text: "Process exited with code 1\nOutput:\nfailed",
      timestamp: "2026-01-01T00:01:00Z",
    });
    expect(isExplorationEligible(failed)).toBe(false);
  });

  it("collapses Read+Grep bursts and keeps Edit primary", () => {
    const model = buildTimelineModel(
      projection([
        event({ id: 1, role: "user", content_text: "go", timestamp: "2026-01-01T00:00:00Z" }),
        event({
          id: 2,
          role: "assistant",
          tool_name: "Read",
          tool_call_id: "r1",
          tool_input_json: { file_path: "/a.ts" },
          timestamp: "2026-01-01T00:00:01Z",
        }),
        event({
          id: 3,
          role: "tool",
          tool_call_id: "r1",
          tool_output_text: "a",
          timestamp: "2026-01-01T00:00:02Z",
        }),
        event({
          id: 4,
          role: "assistant",
          tool_name: "Grep",
          tool_call_id: "g1",
          tool_input_json: { pattern: "x" },
          timestamp: "2026-01-01T00:00:03Z",
        }),
        event({
          id: 5,
          role: "tool",
          tool_call_id: "g1",
          tool_output_text: "hit",
          timestamp: "2026-01-01T00:00:04Z",
        }),
        event({
          id: 6,
          role: "assistant",
          tool_name: "Edit",
          tool_call_id: "e1",
          tool_input_json: { file_path: "/a.ts" },
          timestamp: "2026-01-01T00:00:05Z",
        }),
        event({
          id: 7,
          role: "tool",
          tool_call_id: "e1",
          tool_output_text: "ok",
          timestamp: "2026-01-01T00:00:06Z",
        }),
        event({
          id: 8,
          role: "assistant",
          content_text: "Done.",
          timestamp: "2026-01-01T00:00:07Z",
        }),
      ]),
    );

    expect(model.items.map((item) => item.kind)).toEqual([
      "message",
      "noise_group",
      "tool",
      "message",
    ]);
    const group = model.noiseGroups[0];
    expect(group.interactions.map((i) => i.toolName)).toEqual(["Read", "Grep"]);
    expect(formatExplorationSummary(group.interactions)).toBe("Searched 1 · Read 1");
    expect(model.items[2]).toMatchObject({ kind: "tool", interaction: { toolName: "Edit" } });
  });

  it("keeps a singleton Read as an individual context row", () => {
    const model = buildTimelineModel(
      projection([
        event({ id: 1, role: "user", content_text: "read", timestamp: "2026-01-01T00:00:00Z" }),
        event({
          id: 2,
          role: "assistant",
          tool_name: "Read",
          tool_call_id: "r1",
          timestamp: "2026-01-01T00:00:01Z",
        }),
        event({
          id: 3,
          role: "tool",
          tool_call_id: "r1",
          tool_output_text: "ok",
          timestamp: "2026-01-01T00:00:02Z",
        }),
      ]),
    );
    expect(model.items.map((item) => item.kind)).toEqual(["message", "tool"]);
    expect(model.noiseGroups).toHaveLength(0);
  });

  it("treats co-located assistant prose as a run boundary", () => {
    const model = buildTimelineModel(
      projection([
        event({ id: 1, role: "user", content_text: "go", timestamp: "2026-01-01T00:00:00Z" }),
        event({
          id: 2,
          role: "assistant",
          tool_name: "Grep",
          tool_call_id: "g1",
          tool_input_json: { pattern: "a" },
          timestamp: "2026-01-01T00:00:01Z",
        }),
        event({
          id: 3,
          role: "tool",
          tool_call_id: "g1",
          tool_output_text: "a",
          timestamp: "2026-01-01T00:00:02Z",
        }),
        event({
          id: 4,
          role: "assistant",
          content_text: "Next I will search again.",
          tool_name: "Grep",
          tool_call_id: "g2",
          tool_input_json: { pattern: "b" },
          timestamp: "2026-01-01T00:00:03Z",
        }),
        event({
          id: 5,
          role: "tool",
          tool_call_id: "g2",
          tool_output_text: "b",
          timestamp: "2026-01-01T00:00:04Z",
        }),
        event({
          id: 6,
          role: "assistant",
          tool_name: "Grep",
          tool_call_id: "g3",
          tool_input_json: { pattern: "c" },
          timestamp: "2026-01-01T00:00:05Z",
        }),
        event({
          id: 7,
          role: "tool",
          tool_call_id: "g3",
          tool_output_text: "c",
          timestamp: "2026-01-01T00:00:06Z",
        }),
      ]),
    );
    expect(model.items.map((item) => item.kind)).toEqual([
      "message",
      "tool",
      "message",
      "noise_group",
    ]);
    expect(model.noiseGroups[0].interactions.map((i) => i.toolName)).toEqual(["Grep", "Grep"]);
  });
});
