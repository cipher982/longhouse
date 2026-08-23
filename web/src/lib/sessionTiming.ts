import type { AgentSession } from "../services/api/agents";
import { formatRelativeTime } from "./sessionUtils";

/**
 * How long ago this session started, coarse and static.
 *
 * This used to be a live H:MM:SS counter. It read as a stopwatch on the current
 * turn, but it always measured the whole session, because the per-turn branch
 * it was written for could never fire: `/timeline/sessions/{id}/turns` returns
 * an empty list under the live catalog, so no turn ever looked active. The
 * result was a seconds-resolution clock counting up from the first message
 * forever, which asserted work in progress purely by moving.
 *
 * Session age is orientation, not activity. The runtime strip's own state
 * label and its "Updated ..." stamp own the question of whether anything is
 * happening now.
 */
export function getSessionStartedLabel(
  session: Pick<AgentSession, "started_at"> | null,
  nowMs: number,
): string | null {
  const startedAt = session?.started_at;
  if (!startedAt) return null;

  const relative = formatRelativeTime(startedAt, nowMs);
  // formatRelativeTime's sub-minute wording is a sentence opener; the rest are
  // already suffixes that read correctly after "Started".
  return relative === "Just now" ? "Started just now" : `Started ${relative}`;
}
