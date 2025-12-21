import type { AgentRun } from "../../services/api";

const NBSP = "\u00A0";

export function formatDateTimeShort(iso: string | null | undefined): string {
  if (!iso) {
    return "-";
  }

  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }

  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const day = String(date.getUTCDate()).padStart(2, "0");
  const hours = String(date.getUTCHours()).padStart(2, "0");
  const minutes = String(date.getUTCMinutes()).padStart(2, "0");
  return `${year}-${month}-${day}${NBSP}${hours}:${minutes}`;
}

export function formatStatus(status: string): string {
  switch (status) {
    case "running":
      return "● Running";
    case "processing":
      return "⏳ Processing";
    case "error":
      return "⚠ Error";
    case "idle":
    default:
      return "○ Idle";
  }
}

export function formatDuration(durationMs?: number | null): string {
  if (!durationMs) {
    return "-";
  }
  const secondsTotal = Math.floor(durationMs / 1000);
  const minutes = Math.floor(secondsTotal / 60);
  const seconds = secondsTotal % 60;
  if (minutes > 0) {
    return `${minutes} m ${String(seconds).padStart(2, "0")} s`;
  }
  return `${seconds} s`;
}

export function capitaliseFirst(value: string): string {
  if (!value) {
    return "";
  }
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function formatTokens(tokens?: number | null): string {
  if (tokens === null || tokens === undefined) {
    return "—";
  }
  return tokens.toString();
}

export function formatCost(cost?: number | null): string {
  if (cost === null || cost === undefined) {
    return "—";
  }
  if (cost >= 0.1) {
    return `$${cost.toFixed(2)}`;
  }
  if (cost >= 0.01) {
    return `$${cost.toFixed(3)}`;
  }
  return `$${cost.toFixed(4)}`;
}

export function formatRunStatusIcon(status: AgentRun["status"]): string {
  switch (status) {
    case "running":
      return "▶";
    case "success":
      return "✔";
    case "failed":
      return "✖";
    default:
      return "●";
  }
}
