import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TimelineItem } from "../../../lib/sessionWorkspace";
import { deriveTurnOutline, TurnOutline, type TurnOutlineTurn } from "../TurnOutline";

function userMessageItem(id: number, timestamp: string, text: string): TimelineItem {
  return {
    kind: "message",
    event: {
      id,
      role: "user",
      content_text: text,
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp,
      in_active_context: true,
    },
  };
}

function assistantMessageItem(id: number, timestamp: string, text: string): TimelineItem {
  return {
    kind: "message",
    event: {
      id,
      role: "assistant",
      content_text: text,
      tool_name: null,
      tool_input_json: null,
      tool_output_text: null,
      tool_call_id: null,
      timestamp,
      in_active_context: true,
    },
  };
}

describe("deriveTurnOutline", () => {
  it("starts one turn per user message, ignoring assistant/tool rows", () => {
    const items: TimelineItem[] = [
      userMessageItem(1, "2026-04-15T15:22:00Z", "Investigate and fix the archive upload stall please"),
      assistantMessageItem(2, "2026-04-15T15:22:34Z", "On it."),
      userMessageItem(3, "2026-04-15T16:00:00Z", "Also check the sauron monitor while you're at it"),
    ];
    const turns = deriveTurnOutline(items);
    expect(turns).toHaveLength(2);
    expect(turns[0]).toMatchObject({ eventId: 1, timestamp: "2026-04-15T15:22:00Z" });
    expect(turns[1]).toMatchObject({ eventId: 3, timestamp: "2026-04-15T16:00:00Z" });
  });

  it("clips the ask preview to 48 characters", () => {
    const longAsk =
      "Investigate and fix the archive upload stall — it has been failing silently for a week now";
    const turns = deriveTurnOutline([userMessageItem(1, "2026-04-15T15:22:00Z", longAsk)]);
    expect(turns[0].askPreview).toBe(longAsk.slice(0, 48));
    expect(turns[0].askPreview.length).toBe(48);
  });

  it("returns an empty list when no user message has loaded", () => {
    expect(deriveTurnOutline([assistantMessageItem(1, "2026-04-15T15:22:00Z", "hi")])).toEqual([]);
  });
});

describe("TurnOutline", () => {
  const turns: TurnOutlineTurn[] = [
    { key: "turn-1", eventId: 1, timestamp: "2026-04-15T15:22:00Z", askPreview: "Investigate the stall" },
    { key: "turn-3", eventId: 3, timestamp: "2026-04-15T16:00:00Z", askPreview: "Check the monitor too" },
  ];

  it("renders nothing when there are no turns", () => {
    const onSelectTurn = vi.fn();
    const { container } = render(<TurnOutline turns={[]} onSelectTurn={onSelectTurn} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("calls the scroll handler with the clicked turn", async () => {
    const onSelectTurn = vi.fn();
    render(
      <TurnOutline
        turns={turns}
        runningTurnKey="turn-3"
        currentTurnKey="turn-3"
        onSelectTurn={onSelectTurn}
      />,
    );

    const items = screen.getAllByTestId("session-turn-outline-item");
    expect(items).toHaveLength(2);

    await userEvent.click(items[0]);
    expect(onSelectTurn).toHaveBeenCalledTimes(1);
    expect(onSelectTurn).toHaveBeenCalledWith(turns[0]);
  });

  it("marks the running turn with the ember and the current turn as highlighted", () => {
    render(
      <TurnOutline
        turns={turns}
        runningTurnKey="turn-3"
        currentTurnKey="turn-3"
        onSelectTurn={vi.fn()}
      />,
    );
    const items = screen.getAllByTestId("session-turn-outline-item");
    expect(items[0].querySelector(".session-turn-outline__ember")).toBeNull();
    expect(items[1].querySelector(".session-turn-outline__ember")).not.toBeNull();
    expect(items[1]).toHaveClass("is-current");
    expect(items[1]).toHaveAttribute("aria-current", "true");
  });
});
