/**
 * Phase 4 (Instruments), web-restyle-signal.md. Reads the transcript items
 * the session workspace already has loaded — no fetch — to derive the facts
 * ReadoutRail and the session-header Sparkline show. Delete alongside
 * Sparkline.tsx, ReadoutRail.tsx, activityBuckets.ts and the matching CSS
 * blocks in styles/instruments.css in one commit.
 */
import type { SessionStateFacts } from "@/shared/api/agents";
import type { TimelineItem, ToolInteraction } from "@/shared/session/model";
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
 * Turn start: the server's `turn.started_at`, the newest real user input of
 * the current turn. The header, the composer clock and the readout rail all
 * read it, so they never disagree with each other or with the phone.
 * `session_state.run.started_at` is deliberately NOT this: `run` is the
 * provider process, started once and unchanged across every turn inside it.
 * Only when the server does not know the turn (history from before it
 * tracked one) does this fall back to the newest user message in the loaded
 * thread.
 */
export function getRunningTurnStartMs(
  state: SessionStateFacts | null | undefined,
  items: TimelineItem[],
): number | null {
  const served = state?.turn?.started_at ? Date.parse(state.turn.started_at) : Number.NaN;
  if (Number.isFinite(served)) return served;
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
  /** What the call is for in the transcript's own words, else the tool's
   * status phrase. Can run 60+ characters, so callers must clamp it
   * themselves rather than growing a capsule to fit it. */
  label: string;
  /** True when the label is the call's own headline rather than a phrase
   * for its tool. */
  hasHeadline: boolean;
}

/** The lead running call of the current turn, as the server serves it. */
export function servedRunningCall(state: SessionStateFacts | null | undefined): RunningToolInfo | null {
  const lead = state?.turn?.lead_call;
  if (!lead) return null;
  const headline = lead.headline?.trim();
  return { toolName: lead.tool, label: headline || lead.status, hasHeadline: Boolean(headline) };
}

/** How many tool calls the current turn is running, as the server counts
 * them (parallel calls, subagents); 0 when unknown. */
export function servedRunningCallCount(state: SessionStateFacts | null | undefined): number {
  return state?.turn?.active_call_count ?? 0;
}
