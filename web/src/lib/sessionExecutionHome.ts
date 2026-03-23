import type { SessionExecutionHome } from "../services/api/agents";

export function getExecutionHomeLabel(home: SessionExecutionHome | null | undefined): string | null {
  switch (home) {
    case "managed_local":
      return "On this Mac";
    case "managed_hosted":
      return "Hosted";
    case "cloud_takeover":
      return "Cloud";
    case "legacy":
      return "Legacy";
    default:
      return null;
  }
}

export function isManagedExecutionHome(home: SessionExecutionHome | null | undefined): boolean {
  return home === "managed_local" || home === "managed_hosted";
}
