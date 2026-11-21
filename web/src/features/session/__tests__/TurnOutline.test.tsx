import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TimelineItem } from "@/shared/session/model";
import {
  buildTickMarks,
  deriveTurnOutline,
  formatTurnTime,
  HOVER_OPEN_DELAY_MS,
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

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders nothing when there are fewer than 2 turns", () => {
    const onSelectTurn = vi.fn();
    const empty = render(<TurnOutline turns={[]} onSelectTurn={onSelectTurn} />);
    expect(empty.container).toBeEmptyDOMElement();

    const single = render(<TurnOutline turns={[turns[0]]} onSelectTurn={onSelectTurn} />);
    expect(single.container).toBeEmptyDOMElement();
  });

  it("opens on hover after hover-intent delay, promotes hover-to-click cleanly, and jumps on turn select", () => {
    vi.useFakeTimers();
    const onSelectTurn = vi.fn();
    render(
      <TurnOutline
        turns={turns}
        runningTurnKey="turn-3"
        currentTurnKey="turn-3"
        onSelectTurn={onSelectTurn}
      />,
    );

    const trigger = screen.getByTestId("turn-ticks-trigger");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    // Brief sweep across the rail does not pop the card open.
    fireEvent.mouseEnter(trigger);
    act(() => {
      vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS - 20);
    });
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();
    fireEvent.mouseLeave(trigger);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();

    // Hovering past the intent delay opens the card; clicking promotes it to click-open
    // so leaving the rail does not dismiss it.
    fireEvent.mouseEnter(trigger);
    act(() => {
      vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS + 10);
    });
    expect(screen.getByTestId("session-turn-outline")).toBeInTheDocument();

    fireEvent.click(trigger);
    fireEvent.mouseLeave(trigger);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.getByTestId("session-turn-outline")).toBeInTheDocument();

    const items = screen.getAllByTestId("session-turn-outline-item");
    expect(items).toHaveLength(2);
    expect(items[0].querySelector(".session-turn-outline__ember")).toBeNull();
    expect(items[1].querySelector(".session-turn-outline__ember")).not.toBeNull();
    expect(items[1]).toHaveClass("is-current");
    expect(items[1]).toHaveAttribute("aria-current", "true");

    fireEvent.click(items[0]);
    expect(onSelectTurn).toHaveBeenCalledTimes(1);
    expect(onSelectTurn).toHaveBeenCalledWith(turns[0]);
    expect(screen.queryByTestId("session-turn-outline")).toBeNull();
  });

  it("contains Escape so page-level handlers do not fire and supports pinning from the card", async () => {
    const onPinnedChange = vi.fn();
    const outerEscape = vi.fn();
    window.addEventListener("keydown", outerEscape);
    try {
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
      expect(outerEscape).not.toHaveBeenCalled();

      await userEvent.click(trigger);
      await userEvent.click(screen.getByTestId("turn-outline-pin"));
      expect(onPinnedChange).toHaveBeenCalledWith(true);
      expect(screen.queryByTestId("session-turn-outline")).toBeNull();
    } finally {
      window.removeEventListener("keydown", outerEscape);
    }
  });

  it("forwards pixel and line-mode wheel events over the tick rail to the scroll container", () => {
    const scroller = document.createElement("div");
    const scrollByMock = vi.fn();
    scroller.scrollBy = scrollByMock;

    render(
      <TurnOutline
        turns={turns}
        currentTurnKey="turn-1"
        onSelectTurn={vi.fn()}
        scrollContainer={scroller}
      />,
    );

    const trigger = screen.getByTestId("turn-ticks-trigger");
    const pixelEvent = new WheelEvent("wheel", { deltaY: 48, deltaMode: 0, cancelable: true });
    trigger.dispatchEvent(pixelEvent);
    expect(pixelEvent.defaultPrevented).toBe(true);
    expect(scrollByMock).toHaveBeenCalledWith({ top: 48, left: 0 });

    const lineEvent = new WheelEvent("wheel", { deltaY: 3, deltaMode: 1, cancelable: true });
    trigger.dispatchEvent(lineEvent);
    expect(lineEvent.defaultPrevented).toBe(true);
    expect(scrollByMock).toHaveBeenCalledWith({ top: 48, left: 0 });
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
