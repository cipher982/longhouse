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
