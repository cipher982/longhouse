/**
 * Parse a datetime string as UTC.
 *
 * The backend (SQLite) stores naive datetimes that are actually UTC but
 * serializes them without a "Z" suffix. JavaScript's `new Date()` treats
 * strings without timezone info as local time, which shifts timestamps
 * incorrectly. This helper appends "Z" when no timezone indicator is present.
 */
export function parseUTC(dateStr: string): Date {
  if (!dateStr.endsWith("Z") && !dateStr.includes("+") && !dateStr.includes("-", 10)) {
    return new Date(dateStr + "Z");
  }
  return new Date(dateStr);
}

export function formatRelativeTime(
  dateStr: string,
  nowMs: number = Date.now(),
): string {
  const date = parseUTC(dateStr);
  const diffMs = nowMs - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 30) return `${diffDays}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
