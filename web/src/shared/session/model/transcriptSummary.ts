import type { TimelineItem } from "./types";

export interface TranscriptCounts {
  messages: number;
  toolCalls: number;
}

/**
 * Count the two things a reader actually recognizes in a transcript.
 *
 * The header used to report the raw projection total ("187 entries"), which
 * mixes messages, tool calls and tool results into one number named after an
 * internal unit: a session with 27 messages and 80 tool calls announced 187 of
 * something the reader has no way to picture.
 *
 * Counting the built timeline items rather than the raw projection keeps the
 * header honest about the rows below it — grouped activity still counts every
 * call it contains, and seams and actions are structure, not transcript.
 */
export function countTimelineItems(items: TimelineItem[]): TranscriptCounts {
  let messages = 0;
  let toolCalls = 0;
  for (const item of items) {
    switch (item.kind) {
      case "message":
        messages += 1;
        break;
      case "tool":
        toolCalls += 1;
        break;
      case "activity_group":
        toolCalls += item.group.interactions.length;
        break;
      default:
        break;
    }
  }
  return { messages, toolCalls };
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * Header summary for the loaded transcript, or null when there is nothing to
 * describe and the pane's own empty/loading state should speak instead.
 */
export function formatTranscriptSummary(
  counts: TranscriptCounts,
  { fullyLoaded }: { fullyLoaded: boolean },
): string | null {
  const segments: string[] = [];
  if (counts.messages > 0) segments.push(plural(counts.messages, "message"));
  if (counts.toolCalls > 0) segments.push(plural(counts.toolCalls, "tool call"));
  if (segments.length === 0) return null;

  const summary = segments.join(" · ");
  return fullyLoaded ? summary : `${summary} loaded`;
}
