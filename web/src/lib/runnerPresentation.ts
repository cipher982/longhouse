import type { Runner } from "../services/api";

export type RunnerStatusVariant = "success" | "warning" | "error" | "neutral";

export type RunnerMetadataSummary = {
  platform?: string;
  arch?: string;
  hostname?: string;
  dockerAvailable?: boolean;
};

export function normalizeRunnerMetadata(metadata: unknown): RunnerMetadataSummary | null {
  if (!metadata || typeof metadata !== "object") {
    return null;
  }

  const record = metadata as Record<string, unknown>;
  return {
    platform: typeof record.platform === "string" ? record.platform : undefined,
    arch: typeof record.arch === "string" ? record.arch : undefined,
    hostname: typeof record.hostname === "string" ? record.hostname : undefined,
    dockerAvailable: typeof record.docker_available === "boolean" ? record.docker_available : undefined,
  };
}

export function runnerStatusVariant(status: string): RunnerStatusVariant {
  switch (status) {
    case "online":
      return "success";
    case "offline":
      return "warning";
    case "revoked":
      return "error";
    default:
      return "neutral";
  }
}

export function formatCompactDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds < 60) return `${seconds}s`;

  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  if (minutes < 60) {
    return remSeconds > 0 ? `${minutes}m ${remSeconds}s` : `${minutes}m`;
  }

  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  if (hours < 24) {
    return remMinutes > 0 ? `${hours}h ${remMinutes}m` : `${hours}h`;
  }

  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours > 0 ? `${days}d ${remHours}h` : `${days}d`;
}

export function versionStatusLabel(status: string | null | undefined): string | null {
  switch (status) {
    case "current":
      return "up to date";
    case "outdated":
      return "update available";
    case "ahead":
      return "ahead of latest";
    default:
      return null;
  }
}

export function formatRunnerVersionValue(runner: Pick<Runner, "runner_version" | "latest_runner_version">): string {
  if (runner.runner_version && runner.latest_runner_version && runner.runner_version !== runner.latest_runner_version) {
    return `v${runner.runner_version} (latest v${runner.latest_runner_version})`;
  }
  if (runner.runner_version) {
    return `v${runner.runner_version}`;
  }
  if (runner.latest_runner_version) {
    return `Latest v${runner.latest_runner_version}`;
  }
  return "Unknown";
}

export function updatePolicyLabel(policy: string | null | undefined): string {
  switch (policy) {
    case "apply":
      return "Auto-apply";
    case "off":
      return "Updates off";
    case "notify":
      return "Notify only";
    default:
      return "Policy unknown";
  }
}
