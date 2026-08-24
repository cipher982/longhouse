/**
 * Freshness of the served activity axis.
 *
 * The server projects activity from evidence with a bounded window and stamps
 * `valid_until`. Nothing on the client read it, so a viewer holding a snapshot
 * rendered whatever it last received for as long as the tab stayed open. When
 * no further event arrives -- which is exactly what a wedged turn looks like --
 * a correct server and a pulsing "Working" bar coexist indefinitely.
 *
 * Expired evidence becomes unknown. It never becomes idle, disconnected, or
 * terminated: absence of evidence is not evidence of an ending.
 */
export type ActivityEvidence = {
  state?: string | null;
  valid_until?: string | null;
};

export function activityEvidenceIsLive(activity: ActivityEvidence | null | undefined, nowMs: number): boolean {
  if (!activity) return false;
  // No window is not an expired window. Some sources legitimately omit it, and
  // inventing an expiry would hide live activity.
  if (!activity.valid_until) return true;
  const expiresAtMs = Date.parse(activity.valid_until);
  if (Number.isNaN(expiresAtMs)) return true;
  return nowMs <= expiresAtMs;
}

/** Is the session actively working, according to evidence that is still valid? */
export function isActivityExecuting(activity: ActivityEvidence | null | undefined, nowMs: number): boolean {
  if (!activityEvidenceIsLive(activity, nowMs)) return false;
  return activity?.state === "thinking" || activity?.state === "executing";
}

/** Is the session stalled, according to evidence that is still valid? */
export function isActivityStalled(activity: ActivityEvidence | null | undefined, nowMs: number): boolean {
  if (!activityEvidenceIsLive(activity, nowMs)) return false;
  return activity?.state === "stalled";
}
