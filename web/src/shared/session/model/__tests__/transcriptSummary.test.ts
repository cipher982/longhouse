import { describe, expect, it } from "vitest";
import type { TimelineItem } from "../types";
import { countTimelineItems, formatTranscriptSummary } from "../transcriptSummary";

function message(id: number): TimelineItem {
  return { kind: "message", event: { id } as never };
}

function tool(id: string): TimelineItem {
  return { kind: "tool", interaction: { key: id } as never };
}

function group(size: number): TimelineItem {
  return {
    kind: "activity_group",
    group: {
      key: `group-${size}`,
      interactions: Array.from({ length: size }, (_, index) => ({ key: `t${index}` }) as never),
      timestamp: "2026-03-22T22:00:00Z",
      anchorId: 1 as never,
    },
  };
}

describe("countTimelineItems", () => {
  it("counts grouped activity as the tool calls it contains", () => {
    expect(countTimelineItems([message(1), tool("a"), group(3)])).toEqual({
      messages: 1,
      toolCalls: 4,
    });
  });

  it("treats seams and actions as structure, not transcript", () => {
    const structural: TimelineItem[] = [
      { kind: "seam", seam: {} as never },
      { kind: "action", action: {} as never },
    ];
    expect(countTimelineItems(structural)).toEqual({ messages: 0, toolCalls: 0 });
  });
});

describe("formatTranscriptSummary", () => {
  it("names both things the reader recognizes", () => {
    expect(
      formatTranscriptSummary({ messages: 27, toolCalls: 80 }, { fullyLoaded: true }),
    ).toBe("27 messages · 80 tool calls");
  });

  it("says a page is still arriving instead of understating the session", () => {
    expect(
      formatTranscriptSummary({ messages: 4, toolCalls: 9 }, { fullyLoaded: false }),
    ).toBe("4 messages · 9 tool calls loaded");
  });

  it("drops the segment it has nothing to say about", () => {
    expect(formatTranscriptSummary({ messages: 1, toolCalls: 0 }, { fullyLoaded: true })).toBe(
      "1 message",
    );
    expect(formatTranscriptSummary({ messages: 0, toolCalls: 1 }, { fullyLoaded: true })).toBe(
      "1 tool call",
    );
  });

  it("stays silent when there is nothing loaded to describe", () => {
    expect(
      formatTranscriptSummary({ messages: 0, toolCalls: 0 }, { fullyLoaded: false }),
    ).toBeNull();
  });
});
