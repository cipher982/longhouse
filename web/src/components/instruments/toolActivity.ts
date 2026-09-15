/**
 * Phase 4 (Instruments), web-restyle-signal.md. Reads the transcript items
 * the session workspace already has loaded — no fetch — to derive the facts
 * ReadoutRail and the session-header Sparkline show. Delete alongside
 * Sparkline.tsx, ReadoutRail.tsx, activityBuckets.ts and the matching CSS
 * blocks in styles/instruments.css in one commit.
 */
import type { TimelineItem, ToolInteraction } from "../../lib/sessionWorkspace";
import { getToolSummary, isToolInteractionRunning } from "../../lib/sessionWorkspace";
import { bucketTimestamps } from "./activityBuckets";

/**
 * Tool interactions in `items`, flattening activity-group chips (collapsed
 * "Explored" runs) into their member interactions, in document order.
 */
export function collectToolInteractions(items: TimelineItem[]): ToolInteraction[] {
  const out: ToolInteraction[] = [];
  for (const item of items) {
    if (item.kind === "tool") {
      out.push(item.interaction);
    } else if (item.kind === "activity_group") {
      out.push(...item.group.interactions);
    }
  }
  return out;
}

/** One bucket per minute for the last `windowMinutes` minutes, oldest
 * first — the session header sparkline's data. */
export function bucketToolActivityByMinute(
  items: TimelineItem[],
  nowMs: number,
  windowMinutes = 30,
): number[] {
  const timestamps = collectToolInteractions(items).map((interaction) => interaction.timestamp);
  return bucketTimestamps(timestamps, { nowMs, windowMinutes, bucketMinutes: 1 });
}

/**
 * "This turn" = tool calls since the most recent user message in the loaded
 * thread. Falls back to every loaded tool call when no user message has
 * loaded yet (e.g. a continuation whose first loaded page starts mid-turn).
 */
export function countToolCallsThisTurn(items: TimelineItem[]): number {
  let lastUserIndex = -1;
  items.forEach((item, index) => {
    if (item.kind === "message" && item.event.role === "user") {
      lastUserIndex = index;
    }
  });
  const scope = lastUserIndex >= 0 ? items.slice(lastUserIndex + 1) : items;
  return collectToolInteractions(scope).length;
}

/**
 * The transcript's own summary text for the currently running tool row, if
 * any — the same label the trace shows on that row (never a fabricated
 * one).
 */
export function findRunningToolLabel(items: TimelineItem[]): string | null {
  const running = collectToolInteractions(items).find((interaction) =>
    isToolInteractionRunning(interaction),
  );
  if (!running) return null;
  return getToolSummary(running) || running.toolName || null;
}
