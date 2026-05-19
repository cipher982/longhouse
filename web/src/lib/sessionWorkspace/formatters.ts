import type { AgentEvent, AgentSession } from "../../services/api/agents";
import { parseUTC } from "../dateUtils";

export { getProviderColor, getProviderLabel as formatProviderLabel } from "../providers";

export function formatTime(dateStr: string): string {
  return parseUTC(dateStr).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatFullDate(dateStr: string): string {
  return parseUTC(dateStr).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "In progress";

  const start = parseUTC(startedAt);
  const end = parseUTC(endedAt);
  const diffMs = end.getTime() - start.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return "<1m";
  if (diffMins < 60) return `${diffMins} min`;

  const hours = Math.floor(diffMins / 60);
  const mins = diffMins % 60;
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

export function formatContinuationStamp(dateStr: string): string {
  return parseUTC(dateStr).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function truncatePath(path: string | null, maxLen: number = 50): string {
  if (!path) return "";
  if (path.length <= maxLen) return path;

  const parts = path.split("/");
  if (parts.length <= 3) return "..." + path.slice(-maxLen);
  return "~/" + parts.slice(-3).join("/");
}

export function normalizeSessionOriginLabel(label: string | null | undefined): string | null {
  if (!label) return null;
  if (label === "On this Mac") return "This machine";
  return label;
}

export function getSessionOriginLabel(session: Pick<AgentSession, "origin_label" | "environment">): string {
  return normalizeSessionOriginLabel(session.origin_label) || session.environment || "Local";
}

export function getTimelineMessagePreview(event: AgentEvent): string {
  return event.content_text || "(empty message)";
}
