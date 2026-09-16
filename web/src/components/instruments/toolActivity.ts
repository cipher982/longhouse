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
 * Turn start = the timestamp of the most recent user message in the loaded
 * thread — the one anchor the header, the composer clock, and the readout
 * rail all read now, so the three never disagree (web-restyle-signal item
 * 6). `session_state.run.started_at` is deliberately NOT this: `run` is the
 * underlying provider run's lifecycle (one continuous process, started
 * once, unchanged across every turn inside it), not a per-turn field — using
 * it as a turn anchor is the bug this replaces. The `/turns` list endpoint
 * would be the ideal source but returns empty rows under the live catalog
 * (see `lib/sessionTiming.ts`), so the loaded thread's own last user event is
 * the only fact actually available for this.
 */
export function getRunningTurnStartMs(items: TimelineItem[]): number | null {
  let lastUserMs: number | null = null;
  for (const item of items) {
    if (item.kind === "message" && item.event.role === "user") {
      const ms = Date.parse(item.event.timestamp);
      if (Number.isFinite(ms)) lastUserMs = ms;
    }
  }
  return lastUserMs;
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

export interface RunningToolInfo {
  /** The bare tool name — short enough for the rail's capsule (e.g. "hub"). */
  toolName: string;
  /** The transcript's own summary text for the row, if any — the same label
   * the trace shows on that row (never a fabricated one). Can run 60+
   * characters, so callers must clamp it themselves rather than growing a
   * capsule to fit it. */
  label: string;
}

/** The currently running tool row, if any. */
export function findRunningTool(items: TimelineItem[]): RunningToolInfo | null {
  const running = collectToolInteractions(items).find((interaction) =>
    isToolInteractionRunning(interaction),
  );
  if (!running) return null;
  return { toolName: running.toolName, label: getToolSummary(running) || running.toolName };
}
