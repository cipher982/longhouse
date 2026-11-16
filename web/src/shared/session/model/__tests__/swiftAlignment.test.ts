import { describe, expect, it } from "vitest";
import type { AgentEvent, AgentSessionProjectionItem } from "@/shared/api/agents";
import { buildTimelineModel, getToolExitCode, isActivityEligible, isToolInteractionFailed } from "..";

function ev(partial: Partial<AgentEvent> & { id: number; role: string }): AgentSessionProjectionItem {
  const event = {
    content_text: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp: `2026-11-15T10:00:${String(partial.id).padStart(2, "0")}Z`,
    ...partial,
  } as AgentEvent;
  return { kind: "event", event, timestamp: event.timestamp } as unknown as AgentSessionProjectionItem;
}

const kinds = (items: AgentSessionProjectionItem[]) => buildTimelineModel(items).items.map((i) => i.kind);

describe("timeline model matches the iOS builder (HNT Phase 7 step 1)", () => {
  it("drops an empty assistant row but keeps one carrying text, media or a turn footer", () => {
    expect(kinds([ev({ id: 1, role: "assistant" }), ev({ id: 2, role: "assistant", content_text: "  \n" })])).toEqual([]);
    expect(kinds([ev({ id: 3, role: "assistant", content_text: "hi" })])).toEqual(["message"]);
    expect(kinds([ev({ id: 4, role: "assistant", media_refs: [{ sha256: "a" } as never] })])).toEqual(["message"]);
    expect(
      kinds([ev({ id: 5, role: "assistant", turn_end: { duration_ms: 1, ended_at: "2026-11-15T10:00:05Z" } })]),
    ).toEqual(["message"]);
  });

  it("shows a system row that carries a task notification, waiting agents or an API error", () => {
    const model = buildTimelineModel([
      ev({ id: 1, role: "system", task_notification: { status: "completed" } }),
      ev({ id: 2, role: "system", waiting_background_agents: 2 }),
      ev({ id: 3, role: "system", api_error: { message: "x" } }),
      ev({ id: 4, role: "system", content_text: "plain system text" }),
    ]);
    expect(model.items.map((i) => i.kind)).toEqual(["provider_notification", "provider_notification", "provider_notification"]);
    expect(model.eventIdToSelectionKey.get(1)).toBe("provider_notification:1");
  });

  it("drops empty reasoning without an item fact, keeps withheld reasoning", () => {
    const reasoning = (id: number, extra: Partial<AgentEvent>) =>
      ev({ id, role: "system", interaction_kind: "provider_reasoning", ...extra });
    expect(kinds([reasoning(1, {}), reasoning(2, { content_text: "Thinking:\n  " }), reasoning(3, { content_text: "Thinking:" })])).toEqual([]);
    expect(kinds([reasoning(4, { reasoning_withheld: true })])).toEqual(["reasoning"]);
    expect(kinds([reasoning(5, { content_text: "Thinking:\nreal thought" })])).toEqual(["reasoning"]);
  });

  it("classifies reasoning by item.kind, with no Thinking prefix on the text", () => {
    const row = (id: number, extra: Partial<AgentEvent>) =>
      ev({ id, role: "system", item: { kind: "reasoning", visibility: "full" }, ...extra });
    const model = buildTimelineModel([row(1, { content_text: "real thought" })]);
    expect(model.items.map((i) => i.kind)).toEqual(["reasoning"]);
    expect(model.eventIdToSelectionKey.get(1)).toBe("reasoning:1");
    expect(kinds([row(2, {})])).toEqual([]);
  });

  const pair = (id: number, call: Partial<AgentEvent>, result: Partial<AgentEvent>) => [
    ev({ id, role: "assistant", tool_name: "Read", tool_call_id: `c${id}`, ...call }),
    ev({ id: id + 1, role: "tool", tool_call_id: `c${id}`, tool_output_text: "ok", ...result }),
  ];

  it("keeps a call or result that carries a turn footer out of an activity group", () => {
    const turnEnd = { duration_ms: 1000, ended_at: "2026-11-15T10:00:09Z" };
    const model = buildTimelineModel([...pair(1, {}, {}), ...pair(3, {}, { turn_end: turnEnd }), ...pair(5, {}, {})]);
    expect(model.items.map((i) => i.kind)).toEqual(["tool", "tool", "tool"]);
    const plain = buildTimelineModel([...pair(1, {}, {}), ...pair(3, {}, {})]);
    expect(plain.items.map((i) => i.kind)).toEqual(["activity_group"]);
    expect(isActivityEligible(model.toolItems[1])).toBe(false);
  });

  it("reads a negative exit code as a failure", () => {
    const model = buildTimelineModel(
      pair(1, { tool_name: "Bash" }, { tool_output_text: "Wall time: 1.0 seconds\nProcess exited with code -1\nOutput:\nkilled" }),
    );
    expect(getToolExitCode(model.toolItems[0])).toBe(-1);
    expect(isToolInteractionFailed(model.toolItems[0])).toBe(true);
  });
});
