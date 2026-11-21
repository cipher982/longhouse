import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TimelineItem } from "@/shared/session/model";
import {
  buildTickMarks,
  deriveTurnOutline,
  formatTurnTime,
  MAX_TICK_MARKS,
  mergeTurnOutline,
  TurnOutline,
  type TurnOutlineTurn,
} from "../TurnOutline";

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

describe("formatTurnTime", () => {
  it("prints hour and minute without a day period", () => {
    const text = formatTurnTime("2026-10-06T13:39:00Z");
    expect(text).toMatch(/^\d{1,2}:\d{2}$/);
    expect(text.endsWith(":39")).toBe(true);
  });
});

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

  it("clips the ask preview to 96 characters", () => {
    const longAsk =
      "Investigate and fix the archive upload stall because it has been failing silently for more than a full week across every machine";
    const turns = deriveTurnOutline([userMessageItem(1, "2026-04-15T15:22:00Z", longAsk)]);
    expect(turns[0].askPreview).toBe(longAsk.slice(0, 96));
    expect(turns[0].askPreview.length).toBe(96);
  });

  it("returns an empty list when no user message has loaded", () => {
    expect(deriveTurnOutline([assistantMessageItem(1, "2026-04-15T15:22:00Z", "hi")])).toEqual([]);
  });
});

describe("mergeTurnOutline", () => {
  const indexed = [
    { event_id: "u1", timestamp: "2026-11-14T10:00:00Z", preview: "first ask" },
    { event_id: "u2", timestamp: "2026-11-14T11:00:00Z", preview: '<pasted_content id="p">log</pasted_content> second ask' },
    { event_id: "u3", timestamp: "2026-11-14T12:00:00Z", preview: "rewound away" },
  ];
  const loaded = (id: string, timestamp: string): TurnOutlineTurn => ({
    key: `turn-${id}`,
    eventId: id,
    timestamp,
    askPreview: id,
  });

  it("puts indexed turns older than the loaded window before the loaded turns", () => {
    const turns = mergeTurnOutline(indexed, [loaded("u4", "2026-11-14T13:00:00Z")], Date.parse("2026-11-14T11:30:00Z"));
    expect(turns.map((turn) => turn.eventId)).toEqual(["u1", "u2", "u4"]);
    expect(turns[1].askPreview).toBe("[pasted text] second ask");
  });

  it("lets the loaded window decide inside it, so a rewound turn disappears at once", () => {
    const turns = mergeTurnOutline(indexed, [loaded("u4", "2026-11-14T13:00:00Z")], Date.parse("2026-11-14T11:30:00Z"));
    expect(turns.some((turn) => turn.eventId === "u3")).toBe(false);
  });

  it("uses only the loaded thread when the whole session is loaded, and only the index before anything is", () => {
    const all = [loaded("u1", "2026-11-14T10:00:00Z")];
    expect(mergeTurnOutline(indexed, all, Number.NEGATIVE_INFINITY)).toBe(all);
    expect(mergeTurnOutline(indexed, [], Number.POSITIVE_INFINITY).map((turn) => turn.eventId)).toEqual(["u1", "u2", "u3"]);
  });

  it("keeps a turn that is both indexed and loaded once, as loaded", () => {
    const window = [loaded("u2", "2026-11-14T11:00:00Z")];
    const turns = mergeTurnOutline(indexed, window, Date.parse("2026-11-14T11:00:00Z"));
    expect(turns.map((turn) => turn.eventId)).toEqual(["u1", "u2"]);
    expect(turns[1]).toBe(window[0]);
  });

  it("does not repeat a prompt whose image block opens the loaded window", () => {
    const window = [loaded("u2-image-1", "2026-11-14T11:00:00Z"), loaded("u4", "2026-11-14T13:00:00Z")];
    const turns = mergeTurnOutline(indexed, window, Date.parse("2026-11-14T11:00:00Z"));
    expect(turns.map((turn) => turn.eventId)).toEqual(["u1", "u2", "u4"]);
  });

  it("falls back to the loaded thread without an index", () => {
    const only = [loaded("u9", "2026-11-14T13:00:00Z")];
    expect(mergeTurnOutline(null, only, 0)).toBe(only);
  });
});

describe("buildTickMarks", () => {
  it("keeps 1:1 ticks up to MAX_TICK_MARKS and marks past/current/running accurately", () => {
    const ticks = buildTickMarks(4, 2, 3);
    expect(ticks).toHaveLength(4);
    expect(ticks.map((t) => t.isPast)).toEqual([true, true, false, false]);
    expect(ticks.map((t) => t.isCurrent)).toEqual([false, false, true, false]);
    expect(ticks.map((t) => t.isRunning)).toEqual([false, false, false, true]);
  });

  it("bins sessions longer than MAX_TICK_MARKS cleanly without overflowing", () => {
    const ticks = buildTickMarks(120, 60, 119);
    expect(ticks).toHaveLength(MAX_TICK_MARKS);
    expect(ticks.filter((t) => t.isCurrent)).toHaveLength(1);
    expect(ticks[MAX_TICK_MARKS - 1].isRunning).toBe(true);
  });
});

describe("TurnOutline", () => {
  const turns: TurnOutlineTurn[] = [
    { key: "turn-1", eventId: 1, timestamp: "2026-04-15T15:22:00Z", askPreview: "Investigate the stall" },
    { key: "turn-3", eventId: 3, timestamp: "2026-04-15T16:00:00Z", askPreview: "Check the monitor too" },
  ];

  it("renders nothing when there are fewer than 2 turns", () => {
    const onSelectTurn = vi.fn();
    const empty = render(<TurnOutline turns={[]} onSelectTurn={onSelectTurn} />);
    expect(empty.container).toBeEmptyDOMElement();

    const single = render(<TurnOutline turns={[turns[0]]} onSelectTurn={onSelectTurn} />);
    expect(single.container).toBeEmptyDOMElement();
  });

  it("renders right-edge ticks by default, opens the popover on hover or click, and jumps on turn select", async () => {
    const onSelectTurn = vi.fn();
    const onPinnedChange = vi.fn();
    render(
      <TurnOutline
        turns={turns}
        runningTurnKey="turn-3"
        currentTurnKey="turn-3"
        onSelectTurn={onSelectTurn}
        onPinnedChange={onPinnedChange}
      />,
    );

    const trigger = screen.getByTestId("turn-ticks-trigger");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();

    // Hovering then clicking keeps the popover open (does not flicker shut).
    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    const items = screen.getAllByTestId("session-turn-outline-item");
    expect(items).toHaveLength(2);
    expect(items[0].querySelector(".session-turn-outline__ember")).toBeNull();
    expect(items[1].querySelector(".session-turn-outline__ember")).not.toBeNull();
    expect(items[1]).toHaveClass("is-current");
    expect(items[1]).toHaveAttribute("aria-current", "true");

    await userEvent.click(items[0]);
    expect(onSelectTurn).toHaveBeenCalledTimes(1);
    expect(onSelectTurn).toHaveBeenCalledWith(turns[0]);
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();
  });

  it("dismisses the popover on Escape and allows pinning from the card", async () => {
    const onPinnedChange = vi.fn();
    render(
      <TurnOutline
        turns={turns}
        currentTurnKey="turn-1"
        onSelectTurn={vi.fn()}
        onPinnedChange={onPinnedChange}
      />,
    );

    const trigger = screen.getByTestId("turn-ticks-trigger");
    await userEvent.click(trigger);
    expect(screen.getByTestId("session-turn-outline")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();

    await userEvent.click(trigger);
    await userEvent.click(screen.getByTestId("turn-outline-pin"));
    expect(onPinnedChange).toHaveBeenCalledWith(true);
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();
  });

  it("renders a docked drawer in pinned mode and lets the user unpin it", async () => {
    const onSelectTurn = vi.fn();
    const onPinnedChange = vi.fn();
    render(
      <TurnOutline
        mode="pinned"
        turns={turns}
        runningTurnKey="turn-3"
        currentTurnKey="turn-3"
        onSelectTurn={onSelectTurn}
        onPinnedChange={onPinnedChange}
      />,
    );

    const items = screen.getAllByTestId("session-turn-outline-item");
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveClass("is-current");

    await userEvent.click(items[0]);
    expect(onSelectTurn).toHaveBeenCalledWith(turns[0]);
    // Pinned drawer stays open after jumping.
    expect(screen.getByTestId("session-turn-outline")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("turn-outline-unpin"));
    expect(onPinnedChange).toHaveBeenCalledWith(false);
  });
});
