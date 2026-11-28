import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SessionChecklistStrip } from "../SessionChecklistStrip";
import { buildTimelineModel, isActivityEligible } from "@/shared/session/model/timelineModel";
import type { AgentSession, AgentSessionProjectionItem, SessionChecklist } from "@/shared/api/agents";

function makeChecklist(overrides: Partial<SessionChecklist> = {}): SessionChecklist {
  return {
    items: [
      { id: "1", title: "Kill livelocked git fetch", status: "completed" },
      { id: "2", title: "Gate Kopia backup on battery", status: "in_progress" },
      { id: "3", title: "Check Low Power Mode", status: "pending" },
    ],
    counts: { total: 3, done: 1, in_progress: 1 },
    current_title: "Gate Kopia backup on battery",
    updated_at: "2026-11-28T16:22:25Z",
    provider_tool: "update_steps",
    state: "active",
    ...overrides,
  };
}

describe("SessionChecklistStrip", () => {
  it("renders collapsed by default with count and current step, and expands on click", () => {
    render(<SessionChecklistStrip checklist={makeChecklist()} sessionId="sess-1" />);

    const toggle = screen.getByTestId("session-checklist-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("1/3 steps");
    expect(toggle).toHaveTextContent("Gate Kopia backup on battery");
    expect(screen.queryByTestId("session-checklist-list")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = screen.getByTestId("session-checklist-list");
    expect(list).toHaveTextContent("Kill livelocked git fetch");
    expect(list).toHaveTextContent("Gate Kopia backup on battery");
    expect(list).toHaveTextContent("Check Low Power Mode");
  });

  it("renders complete state cleanly and hides stale checklists", () => {
    const { rerender } = render(
      <SessionChecklistStrip
        checklist={makeChecklist({
          items: [
            { title: "Step A", status: "completed" },
            { title: "Step B", status: "completed" },
          ],
          counts: { total: 2, done: 2, in_progress: 0 },
          current_title: null,
          state: "complete",
        })}
        sessionId="sess-2"
      />,
    );
    expect(screen.getByTestId("session-checklist-toggle")).toHaveTextContent("2/2 steps");
    expect(screen.getByTestId("session-checklist-toggle")).toHaveTextContent("done");

    rerender(
      <SessionChecklistStrip checklist={makeChecklist({ state: "stale" })} sessionId="sess-2" />,
    );
    expect(screen.queryByTestId("session-checklist-strip")).not.toBeInTheDocument();
  });

  it("keeps update_steps out of collapsed activity groups so it never counts as 'other commands'", () => {
    const items: AgentSessionProjectionItem[] = [
      {
        kind: "event",
        session_id: "s1",
        timestamp: "2026-11-28T16:20:07Z",
        event: {
          id: 1,
          role: "assistant",
          content_text: null,
          tool_name: "update_steps",
          tool_input_json: {
            steps: [
              { status: "done", title: "Kill git fetch" },
              { status: "in_progress", title: "Gate Kopia" },
            ],
          },
          tool_output_text: null,
          tool_call_id: "antigravity-1-0",
          tool_intent: "1/2 · Gate Kopia",
          timestamp: "2026-11-28T16:20:07Z",
          in_active_context: true,
        },
      },
      {
        kind: "event",
        session_id: "s1",
        timestamp: "2026-11-28T16:20:08Z",
        event: {
          id: 2,
          role: "tool",
          content_text: null,
          tool_name: "update_steps",
          tool_input_json: null,
          tool_output_text: "OK",
          tool_call_id: "antigravity-1-0",
          timestamp: "2026-11-28T16:20:08Z",
          in_active_context: true,
        },
      },
      {
        kind: "event",
        session_id: "s1",
        timestamp: "2026-11-28T16:20:09Z",
        event: {
          id: 3,
          role: "assistant",
          content_text: null,
          tool_name: "read_file",
          tool_input_json: { Path: "/tmp/a.md" },
          tool_output_text: null,
          tool_call_id: "antigravity-1-1",
          timestamp: "2026-11-28T16:20:09Z",
          in_active_context: true,
        },
      },
      {
        kind: "event",
        session_id: "s1",
        timestamp: "2026-11-28T16:20:10Z",
        event: {
          id: 4,
          role: "tool",
          content_text: null,
          tool_name: "read_file",
          tool_input_json: null,
          tool_output_text: "hello",
          tool_call_id: "antigravity-1-1",
          timestamp: "2026-11-28T16:20:10Z",
          in_active_context: true,
        },
      },
      {
        kind: "event",
        session_id: "s1",
        timestamp: "2026-11-28T16:20:11Z",
        event: {
          id: 5,
          role: "assistant",
          content_text: null,
          tool_name: "read_file",
          tool_input_json: { Path: "/tmp/b.md" },
          tool_output_text: null,
          tool_call_id: "antigravity-1-2",
          timestamp: "2026-11-28T16:20:11Z",
          in_active_context: true,
        },
      },
      {
        kind: "event",
        session_id: "s1",
        timestamp: "2026-11-28T16:20:12Z",
        event: {
          id: 6,
          role: "tool",
          content_text: null,
          tool_name: "read_file",
          tool_input_json: null,
          tool_output_text: "world",
          tool_call_id: "antigravity-1-2",
          timestamp: "2026-11-28T16:20:12Z",
          in_active_context: true,
        },
      },
    ];

    const model = buildTimelineModel(items);
    expect(model.items[0]?.kind).toBe("tool");
    if (model.items[0]?.kind === "tool") {
      expect(model.items[0].interaction.toolName).toBe("update_steps");
      expect(isActivityEligible(model.items[0].interaction)).toBe(false);
    }
    expect(model.items[1]?.kind).toBe("activity_group");
    if (model.items[1]?.kind === "activity_group") {
      expect(model.items[1].group.interactions).toHaveLength(2);
    }
    const _typeCheck: AgentSession["checklist"] = makeChecklist();
    expect(_typeCheck?.counts.total).toBe(3);
  });
});
