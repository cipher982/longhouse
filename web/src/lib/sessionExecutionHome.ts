export function normalizeExecutionVenueLabel(label: string | null | undefined): string | null {
  if (!label) return null;
  if (label === "On this Mac") return "This machine";
  if (label === "Moved to cloud") return "Cloud";
  return label;
}
