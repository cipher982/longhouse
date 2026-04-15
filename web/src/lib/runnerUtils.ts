import { parseUTC } from "./dateUtils";
import { formatCompactDuration } from "./runnerPresentation";
import type { RunnerMetadataSummary } from "./runnerPresentation";
import type { RunnerNativeInstallMode } from "./runnerInstallCommands";
import type { Runner, RunnerDoctorResponse, RunnerJob } from "../services/api";

export function getVersionVariant(status: string | null | undefined): "success" | "warning" | "neutral" {
  switch (status) {
    case "current":
      return "success";
    case "outdated":
      return "warning";
    default:
      return "neutral";
  }
}

export function getJobStatusVariant(status: string): "success" | "warning" | "error" | "neutral" {
  switch (status) {
    case "success":
      return "success";
    case "running":
      return "warning";
    case "failed":
    case "timeout":
    case "canceled":
      return "error";
    default:
      return "neutral";
  }
}

export function formatTimestamp(timestamp: string | null | undefined) {
  if (!timestamp) return "Never";

  const date = parseUTC(timestamp);
  return date.toLocaleString();
}

export function formatRelativeTimestamp(timestamp: string | null | undefined): string {
  if (!timestamp) return "Never";

  const diffMs = Date.now() - parseUTC(timestamp).getTime();
  const diffSeconds = Math.max(0, Math.floor(diffMs / 1000));
  return `${formatCompactDuration(diffSeconds)} ago`;
}

export function formatHeartbeatAge(runner: Runner): string {
  if (typeof runner.last_seen_age_seconds === "number") {
    return `${formatCompactDuration(runner.last_seen_age_seconds)} ago`;
  }

  return formatRelativeTimestamp(runner.last_seen_at);
}

export function formatHeartbeatThreshold(staleAfterSeconds: number | null | undefined): string {
  if (typeof staleAfterSeconds !== "number") {
    return "Unknown";
  }

  return `Stale after ${formatCompactDuration(staleAfterSeconds)}`;
}

export function formatHeartbeatInterval(intervalMs: number | null | undefined): string | null {
  if (typeof intervalMs !== "number") {
    return null;
  }

  return `Heartbeats every ${formatCompactDuration(Math.max(1, Math.round(intervalMs / 1000)))}`;
}

export function formatVersionHint(runner: Runner): string | null {
  switch (runner.version_status) {
    case "current":
      return "Runner binary matches the latest expected build.";
    case "outdated":
      return runner.latest_runner_version
        ? `Upgrade toward v${runner.latest_runner_version}.`
        : "Upgrade the local runner binary.";
    case "ahead":
      return runner.latest_runner_version
        ? `Runner is newer than configured latest v${runner.latest_runner_version}.`
        : "Runner version is newer than the configured latest.";
    default:
      return null;
  }
}

export function updatePolicyHint(policy: string | null | undefined): string {
  switch (policy) {
    case "apply":
      return "Signed updates will apply automatically when the runner is idle.";
    case "off":
      return "Background update checks are disabled on this machine.";
    case "notify":
      return "Runner reports update availability and waits for a manual apply.";
    default:
      return "Runner has not reported its auto-update policy yet.";
  }
}

export function installLayoutLabel(runner: Runner): string {
  if (runner.managed_install_ready) {
    return runner.install_layout_version ? `Managed layout v${runner.install_layout_version}` : "Managed layout";
  }
  return "Legacy layout";
}

export function installLayoutHint(runner: Runner): string {
  if (runner.managed_install_ready) {
    return "This machine can use signed update apply and background auto-update.";
  }
  return "Re-run the installer once to migrate this machine onto the managed versioned layout.";
}

export function capabilitySyncLabel(runner: Runner): string {
  if (runner.capabilities_match === true) {
    return "Aligned";
  }
  if (runner.capabilities_match === false) {
    return "Mismatch";
  }
  if (runner.reported_capabilities && runner.reported_capabilities.length > 0) {
    return "Reported";
  }
  return "Unknown";
}

export function capabilitySyncHint(runner: Runner): string | null {
  if (runner.capabilities_match === true) {
    return "Local runner capabilities match Longhouse configuration.";
  }
  if (runner.capabilities_match === false) {
    return "Local runner capabilities differ from Longhouse configuration.";
  }
  if (runner.reported_capabilities && runner.reported_capabilities.length > 0) {
    return "Runner reported capabilities, but no comparison result is available yet.";
  }
  return "Runner has not reported capabilities yet.";
}

export function jobDuration(job: RunnerJob): string | null {
  if (!job.started_at || !job.finished_at) {
    return null;
  }

  const start = parseUTC(job.started_at).getTime();
  const end = parseUTC(job.finished_at).getTime();
  const diffSeconds = Math.max(0, Math.round((end - start) / 1000));
  return formatCompactDuration(diffSeconds);
}

export function jobPreview(job: RunnerJob): string | null {
  const text = job.error || job.stderr_trunc;
  if (!text) {
    return null;
  }

  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) {
    return null;
  }

  return normalized.length > 220 ? `${normalized.slice(0, 217)}...` : normalized;
}

export function defaultRepairMode(doctor: RunnerDoctorResponse | undefined, metadata: RunnerMetadataSummary | null): RunnerNativeInstallMode {
  if (doctor?.repair_install_mode === "server" || doctor?.repair_install_mode === "desktop") {
    return doctor.repair_install_mode;
  }
  return metadata?.platform === "darwin" ? "desktop" : "server";
}
