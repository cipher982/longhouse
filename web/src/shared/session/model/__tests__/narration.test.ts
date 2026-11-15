import { describe, expect, it } from "vitest";
import type { AgentEvent } from "@/shared/api/agents";
import { messagePhase, narrationTrail, timelineItemIdentity } from "../timelineModel";
import type { TimelineItem, ToolInteraction } from "../types";

function event(id: number, role: string, extra: Partial<AgentEvent> = {}): AgentEvent {
  return { id, role, content_text: "text", timestamp: "2026-11-15T12:00:00Z", ...extra } as AgentEvent;
}

function message(id: number, role: string, extra: Partial<AgentEvent> = {}): TimelineItem {
  return { kind: "message", event: event(id, role, extra) };
}

function tool(key: string, toolName = "Read"): TimelineItem {
  return { kind: "tool", interaction: { key, toolName, anchorId: 1 } as unknown as ToolInteraction };
}

describe("messagePhase", () => {
  it("reads the provider label, then the normalizer's item fact, and never guesses", () => {
    expect(messagePhase(event(1, "assistant", { message_phase: "final_answer" }))).toBe("final_answer");
    expect(messagePhase(event(1, "assistant", { item: { phase: "commentary", phase_source: "derived" } }))).toBe("commentary");
    expect(messagePhase(event(1, "assistant", { item: { phase: "final", phase_source: "derived" } }))).toBe("final_answer");
    expect(messagePhase(event(1, "assistant"))).toBeNull();
    expect(messagePhase(event(1, "user", { item: { phase: "commentary", phase_source: "derived" } }))).toBeNull();
  });
});

describe("narrationTrail", () => {
  const commentary = { item: { phase: "commentary", phase_source: "derived" } } as Partial<AgentEvent>;

  it("trails the tools after commentary text under it, up to the answer", () => {
    const items = [
      message(1, "user"),
      message(2, "assistant", commentary),
      tool("a"),
      tool("b"),
      message(3, "assistant", { item: { phase: "final", phase_source: "derived" } }),
      tool("c"),
    ];
    const trail = narrationTrail(items);
    expect([...trail]).toEqual([timelineItemIdentity(items[2]), timelineItemIdentity(items[3])]);
  });

  it("trails nothing when the phase is unknown, so older renders read as before", () => {
    expect(narrationTrail([message(2, "assistant"), tool("a"), tool("b")]).size).toBe(0);
  });

  it("keeps a question to the user a full row and stops the trail there", () => {
    const items = [message(2, "assistant", commentary), tool("q", "AskUserQuestion"), tool("a")];
    expect(narrationTrail(items).size).toBe(0);
  });

  it("still trails tools after a thought", () => {
    const items: TimelineItem[] = [{ kind: "reasoning", event: event(5, "system") }, tool("a")];
    expect(narrationTrail(items).has(timelineItemIdentity(items[1]))).toBe(true);
  });
});
