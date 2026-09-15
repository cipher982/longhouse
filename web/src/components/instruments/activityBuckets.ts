/**
 * Phase 4 (Instruments), web-restyle-signal.md. Part of the Sparkline
 * instrument — delete alongside Sparkline.tsx, toolActivity.ts and the
 * "Sparkline" CSS block in styles/instruments.css in one commit.
 */

export interface BucketWindow {
  /** The clock reading the buckets end at — always `Date.now()` (or a value
   * derived from it), never `new Date()`, so frozen-clock captures bucket
   * correctly. */
  nowMs: number;
  windowMinutes: number;
  bucketMinutes: number;
}

/**
 * Fixed-width buckets ending at `nowMs`, oldest first — the shape Sparkline
 * expects. Always returns `windowMinutes / bucketMinutes` buckets (rounded),
 * so a burst of very recent activity still draws a full-width line instead
 * of a truncated one, and timestamps outside the window (including ones in
 * the future relative to `nowMs`) are dropped rather than clamped in.
 */
export function bucketTimestamps(
  timestamps: Array<string | null | undefined>,
  { nowMs, windowMinutes, bucketMinutes }: BucketWindow,
): number[] {
  const bucketCount = Math.max(1, Math.round(windowMinutes / bucketMinutes));
  const bucketMs = bucketMinutes * 60_000;
  const windowStart = nowMs - bucketCount * bucketMs;
  const buckets = new Array(bucketCount).fill(0) as number[];

  for (const ts of timestamps) {
    if (!ts) continue;
    const ms = Date.parse(ts);
    if (!Number.isFinite(ms) || ms < windowStart || ms > nowMs) continue;
    const index = Math.min(bucketCount - 1, Math.floor((ms - windowStart) / bucketMs));
    buckets[index] += 1;
  }

  return buckets;
}
