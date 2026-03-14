import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useCreateEnrollToken,
  useRunner,
  useRunnerDoctor,
  useRunnerJobs,
  useRevokeRunner,
  useRotateRunnerSecret,
  useUpdateRunner,
} from "../hooks/useRunners";
import { useConfirm } from "../components/confirm";
import {
  Badge,
  Button,
  EmptyState,
  PageShell,
  SectionHeader,
  Spinner
} from "../components/ui";
import { parseUTC } from "../lib/dateUtils";
import {
  buildRunnerNativeInstallCommand,
  describeRunnerNativeInstallMode,
  type RunnerNativeInstallMode,
} from "../lib/runnerInstallCommands";
import type { Runner, RunnerDoctorResponse, RunnerJob } from "../services/api";
import "../styles/runner-detail.css";

type RunnerMetadataSummary = {
  platform?: string;
  arch?: string;
  hostname?: string;
  dockerAvailable?: boolean;
};

function getStatusVariant(status: string): "success" | "warning" | "error" | "neutral" {
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

function getVersionVariant(status: string | null | undefined): "success" | "warning" | "neutral" {
  switch (status) {
    case "current":
      return "success";
    case "outdated":
      return "warning";
    default:
      return "neutral";
  }
}

function getJobStatusVariant(status: string): "success" | "warning" | "error" | "neutral" {
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

function formatCompactDuration(totalSeconds: number): string {
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

function formatTimestamp(timestamp: string | null | undefined) {
  if (!timestamp) return "Never";

  const date = parseUTC(timestamp);
  return date.toLocaleString();
}

function formatRelativeTimestamp(timestamp: string | null | undefined): string {
  if (!timestamp) return "Never";

  const diffMs = Date.now() - parseUTC(timestamp).getTime();
  const diffSeconds = Math.max(0, Math.floor(diffMs / 1000));
  return `${formatCompactDuration(diffSeconds)} ago`;
}

function formatHeartbeatAge(runner: Runner): string {
  if (typeof runner.last_seen_age_seconds === "number") {
    return `${formatCompactDuration(runner.last_seen_age_seconds)} ago`;
  }

  return formatRelativeTimestamp(runner.last_seen_at);
}

function formatHeartbeatThreshold(staleAfterSeconds: number | null | undefined): string {
  if (typeof staleAfterSeconds !== "number") {
    return "Unknown";
  }

  return `Stale after ${formatCompactDuration(staleAfterSeconds)}`;
}

function formatHeartbeatInterval(intervalMs: number | null | undefined): string | null {
  if (typeof intervalMs !== "number") {
    return null;
  }

  return `Heartbeats every ${formatCompactDuration(Math.max(1, Math.round(intervalMs / 1000)))}`;
}

function versionStatusLabel(status: string | null | undefined): string | null {
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

function formatVersionValue(runner: Runner): string {
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

function formatVersionHint(runner: Runner): string | null {
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

function capabilitySyncLabel(runner: Runner): string {
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

function capabilitySyncHint(runner: Runner): string | null {
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

function jobDuration(job: RunnerJob): string | null {
  if (!job.started_at || !job.finished_at) {
    return null;
  }

  const start = parseUTC(job.started_at).getTime();
  const end = parseUTC(job.finished_at).getTime();
  const diffSeconds = Math.max(0, Math.round((end - start) / 1000));
  return formatCompactDuration(diffSeconds);
}

function jobPreview(job: RunnerJob): string | null {
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

function normalizeRunnerMetadata(metadata: unknown): RunnerMetadataSummary | null {
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

function defaultRepairMode(doctor: RunnerDoctorResponse | undefined, metadata: RunnerMetadataSummary | null): RunnerNativeInstallMode {
  if (doctor?.repair_install_mode === "server" || doctor?.repair_install_mode === "desktop") {
    return doctor.repair_install_mode;
  }
  return metadata?.platform === "darwin" ? "desktop" : "server";
}

export default function RunnerDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const runnerId = id ? parseInt(id, 10) : 0;
  const confirm = useConfirm();

  const { data: runner, isLoading, error } = useRunner(runnerId, { refetchInterval: 10_000 });
  const {
    data: recentJobs,
    isLoading: jobsLoading,
    error: jobsError,
  } = useRunnerJobs(runnerId, { limit: 6, refetchInterval: 15_000 });
  const runnerMetadata = normalizeRunnerMetadata(runner?.runner_metadata);
  const updateRunnerMutation = useUpdateRunner();
  const revokeRunnerMutation = useRevokeRunner();
  const rotateSecretMutation = useRotateRunnerSecret();
  const doctorMutation = useRunnerDoctor();
  const repairTokenMutation = useCreateEnrollToken();

  const [isEditingCapabilities, setIsEditingCapabilities] = useState(false);
  const [selectedCapabilities, setSelectedCapabilities] = useState<string[]>([]);
  const [rotatedSecret, setRotatedSecret] = useState<string | null>(null);
  const [secretCopied, setSecretCopied] = useState(false);
  const [repairCopied, setRepairCopied] = useState(false);
  const [repairMode, setRepairMode] = useState<RunnerNativeInstallMode>("desktop");
  const [actionError, setActionError] = useState<string | null>(null);

  const allCapabilities = [
    { id: "exec.readonly", label: "Read-only execution", description: "Can run read-only commands" },
    { id: "exec.full", label: "Full execution", description: "Can run any command" },
    { id: "docker", label: "Docker access", description: "Can execute Docker commands" },
  ];

  const handleEditCapabilities = () => {
    setSelectedCapabilities(runner?.capabilities || []);
    setIsEditingCapabilities(true);
  };

  const handleSaveCapabilities = async () => {
    setActionError(null);
    try {
      await updateRunnerMutation.mutateAsync({
        id: runnerId,
        data: { capabilities: selectedCapabilities },
      });
      setIsEditingCapabilities(false);
    } catch (err) {
      console.error("Failed to update capabilities:", err);
      setActionError("Failed to update capabilities. Please try again.");
    }
  };

  const handleRunDoctor = async () => {
    setActionError(null);
    setRepairCopied(false);
    try {
      const result = await doctorMutation.mutateAsync(runnerId);
      setRepairMode(defaultRepairMode(result, runnerMetadata));
    } catch (err) {
      console.error("Failed to run doctor:", err);
      setActionError("Failed to run doctor. Please try again.");
    }
  };

  const handleGenerateRepairCommand = async () => {
    setActionError(null);
    setRepairCopied(false);
    try {
      await repairTokenMutation.mutateAsync();
    } catch (err) {
      console.error("Failed to create repair command:", err);
      setActionError("Failed to generate a repair command. Please try again.");
    }
  };

  const handleCopyRepairCommand = async () => {
    const repairCommand = getRepairCommand();
    if (!repairCommand) {
      return;
    }

    try {
      await navigator.clipboard.writeText(repairCommand);
      setRepairCopied(true);
      setTimeout(() => setRepairCopied(false), 2000);
    } catch (error) {
      console.error("Failed to copy repair command:", error);
    }
  };

  const handleRevoke = async () => {
    const confirmed = await confirm({
      title: `Revoke runner "${runner?.name}"?`,
      message: 'This runner will no longer be able to connect. You can create a new runner if needed.',
      confirmLabel: 'Revoke',
      cancelLabel: 'Keep',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }

    setActionError(null);
    try {
      await revokeRunnerMutation.mutateAsync(runnerId);
      navigate("/runners");
    } catch (err) {
      console.error("Failed to revoke runner:", err);
      setActionError("Failed to revoke runner. Please try again.");
    }
  };

  const handleRotateSecret = async () => {
    const confirmed = await confirm({
      title: `Rotate secret for "${runner?.name}"?`,
      message: 'This will generate a new secret (old secret becomes invalid), disconnect the runner if connected, and you\'ll need to update the runner with the new secret.',
      confirmLabel: 'Rotate Secret',
      cancelLabel: 'Keep Current',
      variant: 'warning',
    });
    if (!confirmed) {
      return;
    }

    setRotatedSecret(null);
    setSecretCopied(false);
    setActionError(null);

    try {
      const result = await rotateSecretMutation.mutateAsync(runnerId);
      setRotatedSecret(result.runner_secret);
    } catch (err) {
      console.error("Failed to rotate secret:", err);
      setActionError("Failed to rotate secret. Please try again.");
    }
  };

  const handleCopySecret = async () => {
    if (rotatedSecret) {
      try {
        await navigator.clipboard.writeText(rotatedSecret);
        setSecretCopied(true);
        setTimeout(() => setSecretCopied(false), 2000);
      } catch (error) {
        console.error("Failed to copy:", error);
      }
    }
  };

  const toggleCapability = (capId: string) => {
    setSelectedCapabilities((prev) =>
      prev.includes(capId)
        ? prev.filter((c) => c !== capId)
        : [...prev, capId]
    );
  };

  const getDoctorVariant = (severity: RunnerDoctorResponse["severity"]): "success" | "warning" | "error" => {
    switch (severity) {
      case "healthy":
        return "success";
      case "warning":
        return "warning";
      default:
        return "error";
    }
  };

  const getRepairCommand = () => {
    if (!repairTokenMutation.data || !runner) {
      return "";
    }

    return buildRunnerNativeInstallCommand({
      enrollToken: repairTokenMutation.data.enroll_token,
      longhouseUrl: repairTokenMutation.data.longhouse_url,
      oneLinerInstallCommand: repairTokenMutation.data.one_liner_install_command,
      runnerName: runner.name,
    }, repairMode);
  };

  if (isLoading) {
    return (
      <PageShell size="wide" className="runner-detail-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading runner..."
          description="Fetching runner details."
        />
      </PageShell>
    );
  }

  if (error || !runner) {
    return (
      <PageShell size="wide" className="runner-detail-container">
        <EmptyState
          variant="error"
          title="Runner not found"
          description="This runner might have been revoked or removed."
          action={
            <Button variant="secondary" onClick={() => navigate("/runners")}>
              Back to Runners
            </Button>
          }
        />
      </PageShell>
    );
  }

  return (
    <PageShell size="wide" className="runner-detail-container">
      <div className="runner-detail-page">
        <SectionHeader
          title={runner.name}
          description={runner.status_summary ?? "Manage runner capabilities, secrets, and status."}
          actions={
            <div className="runner-detail-header-actions">
              <Badge variant={getStatusVariant(runner.status)}>{runner.status}</Badge>
              {runner.status_reason && (
                <Badge variant="neutral" className="runner-code-badge">
                  {runner.status_reason}
                </Badge>
              )}
              {versionStatusLabel(runner.version_status) && (
                <Badge variant={getVersionVariant(runner.version_status)}>
                  {versionStatusLabel(runner.version_status)}
                </Badge>
              )}
              <Button variant="ghost" size="sm" onClick={() => navigate("/runners")}>
                ← Back
              </Button>
            </div>
          }
        />

        {actionError && (
          <div className="ui-action-error-banner" role="alert">
            {actionError}
            <button type="button" className="ui-action-error-dismiss" onClick={() => setActionError(null)}>
              Dismiss
            </button>
          </div>
        )}

        <div className="runner-detail-sections">
          <section className="detail-section">
            <div className="section-header">
              <div>
                <h2 className="ui-section-title">Health</h2>
                <p className="detail-help-text">Derived from runner heartbeats, reported metadata, and Longhouse policy.</p>
              </div>
            </div>

            <div className={`runner-health-summary runner-health-summary--${runner.status}`}>
              <div className="runner-health-summary-badges">
                <Badge variant={getStatusVariant(runner.status)}>{runner.status}</Badge>
                {versionStatusLabel(runner.version_status) && (
                  <Badge variant={getVersionVariant(runner.version_status)}>
                    {versionStatusLabel(runner.version_status)}
                  </Badge>
                )}
                {runner.capabilities_match === false && (
                  <Badge variant="warning">capability mismatch</Badge>
                )}
              </div>
              <strong>{runner.status_summary ?? "No health summary reported."}</strong>
              <span className="runner-health-summary-note">
                {runner.last_seen_at
                  ? `Last heartbeat ${formatHeartbeatAge(runner)}.`
                  : "No heartbeat has been recorded yet."}
              </span>
            </div>

            <div className="detail-grid">
              <div className="detail-item">
                <span className="detail-label">Reason Code</span>
                <div className="detail-value-stack">
                  <span className="detail-value detail-value--code">{runner.status_reason ?? "unknown"}</span>
                </div>
              </div>
              <div className="detail-item">
                <span className="detail-label">Last Heartbeat</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{formatHeartbeatAge(runner)}</span>
                  <span className="detail-subvalue">{formatTimestamp(runner.last_seen_at)}</span>
                </div>
              </div>
              <div className="detail-item">
                <span className="detail-label">Heartbeat Window</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{formatHeartbeatThreshold(runner.stale_after_seconds)}</span>
                  {formatHeartbeatInterval(runner.heartbeat_interval_ms) && (
                    <span className="detail-subvalue">{formatHeartbeatInterval(runner.heartbeat_interval_ms)}</span>
                  )}
                </div>
              </div>
              <div className="detail-item">
                <span className="detail-label">Install Mode</span>
                <span className="detail-value">{runner.install_mode ?? "Unknown"}</span>
              </div>
              <div className="detail-item">
                <span className="detail-label">Version</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{formatVersionValue(runner)}</span>
                  {formatVersionHint(runner) && (
                    <span className="detail-subvalue">{formatVersionHint(runner)}</span>
                  )}
                </div>
              </div>
              <div className="detail-item">
                <span className="detail-label">Capability Sync</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{capabilitySyncLabel(runner)}</span>
                  <span className="detail-subvalue">{capabilitySyncHint(runner)}</span>
                </div>
              </div>
            </div>

            <div className="runner-health-capability-grid">
              <div className="runner-health-capability-group">
                <span className="detail-label">Longhouse capabilities</span>
                {runner.capabilities && runner.capabilities.length > 0 ? (
                  <div className="capabilities-list">
                    {runner.capabilities.map((cap) => (
                      <span key={cap} className="capability-chip">
                        {cap}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="no-capabilities">No capabilities configured</p>
                )}
              </div>

              <div className="runner-health-capability-group">
                <span className="detail-label">Runner reported</span>
                {runner.reported_capabilities && runner.reported_capabilities.length > 0 ? (
                  <div className="capabilities-list">
                    {runner.reported_capabilities.map((cap) => (
                      <span key={cap} className="capability-chip capability-chip--reported">
                        {cap}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="no-capabilities">No capability report yet</p>
                )}
              </div>
            </div>
          </section>

          <section className="detail-section">
            <h2 className="ui-section-title">Information</h2>
            <div className="detail-grid">
              <div className="detail-item">
                <span className="detail-label">Runner ID:</span>
                <span className="detail-value">{runner.id}</span>
              </div>
              <div className="detail-item">
                <span className="detail-label">Created:</span>
                <span className="detail-value">{formatTimestamp(runner.created_at)}</span>
              </div>
              <div className="detail-item">
                <span className="detail-label">Last Seen:</span>
                <span className="detail-value">{formatTimestamp(runner.last_seen_at)}</span>
              </div>
              <div className="detail-item">
                <span className="detail-label">Updated:</span>
                <span className="detail-value">{formatTimestamp(runner.updated_at)}</span>
              </div>
              {runnerMetadata && (
                <>
                  <div className="detail-item">
                    <span className="detail-label">Platform:</span>
                    <span className="detail-value">{runnerMetadata.platform ?? "Unknown"}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Architecture:</span>
                    <span className="detail-value">{runnerMetadata.arch ?? "Unknown"}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Hostname:</span>
                    <span className="detail-value">{runnerMetadata.hostname ?? "Unknown"}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Docker Available:</span>
                    <span className="detail-value">
                      {runnerMetadata.dockerAvailable === undefined ? "Unknown" : runnerMetadata.dockerAvailable ? "Yes" : "No"}
                    </span>
                  </div>
                </>
              )}
            </div>
          </section>

          <section className="detail-section">
            <div className="section-header">
              <div>
                <h2 className="ui-section-title">Recent Jobs</h2>
                <p className="detail-help-text">Latest commands dispatched to this runner.</p>
              </div>
            </div>

            {jobsLoading && (
              <div className="doctor-loading">
                <Spinner size="md" />
                <span>Loading recent jobs...</span>
              </div>
            )}

            {!jobsLoading && jobsError && (
              <p className="no-capabilities">
                {jobsError instanceof Error ? jobsError.message : "Failed to load recent jobs."}
              </p>
            )}

            {!jobsLoading && !jobsError && (!recentJobs || recentJobs.length === 0) && (
              <p className="no-capabilities">No jobs have been dispatched to this runner yet.</p>
            )}

            {!jobsLoading && !jobsError && recentJobs && recentJobs.length > 0 && (
              <div className="runner-job-list" data-testid="runner-jobs-section">
                {recentJobs.map((job) => (
                  <div key={job.id} className={`runner-job-item runner-job-item--${job.status}`}>
                    <div className="runner-job-header">
                      <div className="runner-job-copy">
                        <code className="runner-job-command">{job.command}</code>
                        <div className="runner-job-meta">
                          <span>{formatRelativeTimestamp(job.created_at)}</span>
                          {job.started_at && <span>started {formatTimestamp(job.started_at)}</span>}
                          {job.finished_at && <span>finished {formatTimestamp(job.finished_at)}</span>}
                        </div>
                      </div>
                      <Badge variant={getJobStatusVariant(job.status)}>{job.status}</Badge>
                    </div>

                    <div className="runner-job-facts">
                      <span>Timeout {formatCompactDuration(job.timeout_secs)}</span>
                      {job.exit_code !== null && job.exit_code !== undefined && (
                        <span>Exit {job.exit_code}</span>
                      )}
                      {jobDuration(job) && <span>Runtime {jobDuration(job)}</span>}
                    </div>

                    {jobPreview(job) && (
                      <p className="runner-job-preview">{jobPreview(job)}</p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="detail-section">
            <div className="section-header">
              <div>
                <h2 className="ui-section-title">Doctor</h2>
                <p className="detail-help-text">Diagnose runner health and generate the right repair command for this machine.</p>
              </div>
              <Button variant="secondary" size="sm" onClick={handleRunDoctor} disabled={doctorMutation.isPending}>
                {doctorMutation.isPending ? "Running..." : "Run Doctor"}
              </Button>
            </div>

            {!doctorMutation.data && !doctorMutation.isPending && (
              <p className="no-capabilities">Run Doctor to see the current diagnosis and recommended next step.</p>
            )}

            {doctorMutation.isPending && (
              <div className="doctor-loading">
                <Spinner size="md" />
                <span>Inspecting runner health...</span>
              </div>
            )}

            {doctorMutation.data && (
              <div className="doctor-panel" data-testid="runner-doctor-panel">
                <div className="doctor-summary-row">
                  <Badge variant={getDoctorVariant(doctorMutation.data.severity)}>{doctorMutation.data.severity}</Badge>
                  <div className="doctor-summary-copy">
                    <strong>{doctorMutation.data.summary}</strong>
                    <span>{doctorMutation.data.recommended_action}</span>
                  </div>
                </div>

                <div className="detail-grid doctor-meta-grid">
                  <div className="detail-item">
                    <span className="detail-label">Reason Code</span>
                    <span className="detail-value doctor-code">{doctorMutation.data.reason_code}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Install Mode</span>
                    <span className="detail-value">{doctorMutation.data.install_mode ?? "Unknown"}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Repair Command</span>
                    <span className="detail-value">{doctorMutation.data.repair_supported ? "Available" : "Not needed"}</span>
                  </div>
                </div>

                <div className="doctor-check-list">
                  {(doctorMutation.data.checks ?? []).map((check: NonNullable<RunnerDoctorResponse["checks"]>[number]) => (
                    <div key={check.key} className={`doctor-check doctor-check--${check.status}`}>
                      <div className="doctor-check-header">
                        <strong>{check.label}</strong>
                        <span className="doctor-check-status">{check.status}</span>
                      </div>
                      <p>{check.message}</p>
                    </div>
                  ))}
                </div>

                {doctorMutation.data.repair_supported && (
                  <div className="doctor-repair-panel">
                    <div className="doctor-repair-header">
                      <div>
                        <h3>Repair Command</h3>
                        <p className="detail-help-text">This re-enrolls the existing runner name and refreshes its local install.</p>
                      </div>
                    </div>

                    {runnerMetadata?.platform !== "darwin" && (
                      <>
                        <span className="detail-label">Machine Type</span>
                        <div className="doctor-mode-toggle">
                          <button
                            type="button"
                            className={`doctor-mode-button${repairMode === "desktop" ? " doctor-mode-button--active" : ""}`}
                            onClick={() => setRepairMode("desktop")}
                          >
                            Desktop / Laptop
                          </button>
                          <button
                            type="button"
                            className={`doctor-mode-button${repairMode === "server" ? " doctor-mode-button--active" : ""}`}
                            onClick={() => setRepairMode("server")}
                          >
                            Always-on Linux Server
                          </button>
                        </div>
                        <p className="detail-help-text">{describeRunnerNativeInstallMode(repairMode)}</p>
                      </>
                    )}

                    <div className="doctor-repair-actions">
                      <Button variant="primary" onClick={handleGenerateRepairCommand} disabled={repairTokenMutation.isPending}>
                        {repairTokenMutation.isPending ? "Generating..." : "Generate Repair Command"}
                      </Button>
                      {repairTokenMutation.data && (
                        <Button variant="secondary" onClick={handleCopyRepairCommand}>
                          {repairCopied ? "Copied!" : "Copy Command"}
                        </Button>
                      )}
                    </div>

                    {repairTokenMutation.data && (
                      <pre className="doctor-command-block"><code>{getRepairCommand()}</code></pre>
                    )}
                  </div>
                )}
              </div>
            )}
          </section>

          <section className="detail-section">
            <div className="section-header">
              <h2 className="ui-section-title">Capabilities</h2>
              {!isEditingCapabilities && runner.status !== "revoked" && (
                <Button variant="secondary" size="sm" onClick={handleEditCapabilities}>
                  Edit
                </Button>
              )}
            </div>

            {isEditingCapabilities ? (
              <div className="capabilities-edit">
                {allCapabilities.map((cap) => (
                  <label key={cap.id} className="capability-checkbox">
                    <input
                      type="checkbox"
                      checked={selectedCapabilities.includes(cap.id)}
                      onChange={() => toggleCapability(cap.id)}
                    />
                    <div className="capability-info">
                      <span className="capability-name">{cap.label}</span>
                      <span className="capability-description">{cap.description}</span>
                    </div>
                  </label>
                ))}

                <div className="capabilities-actions">
                  <Button
                    variant="success"
                    onClick={handleSaveCapabilities}
                    disabled={updateRunnerMutation.isPending}
                  >
                    {updateRunnerMutation.isPending ? "Saving..." : "Save"}
                  </Button>
                  <Button variant="ghost" onClick={() => setIsEditingCapabilities(false)}>
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <div className="capabilities-display">
                {runner.capabilities && runner.capabilities.length > 0 ? (
                  <div className="capabilities-list">
                    {runner.capabilities.map((cap) => (
                      <span key={cap} className="capability-chip">
                        {cap}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="no-capabilities">No capabilities configured</p>
                )}
              </div>
            )}
          </section>

          {runner.status !== "revoked" && (
            <section className="detail-section danger-section">
              <h2 className="ui-section-title">Danger Zone</h2>

              <div className="danger-item">
                <div className="danger-item-header">
                  <div className="danger-item-info">
                    <h3>Rotate Secret</h3>
                    <p className="danger-description">
                      Generate a new secret, invalidating the old one. The runner will be disconnected.
                    </p>
                  </div>
                  <Button
                    variant="tertiary"
                    className="runner-warning-button"
                    onClick={handleRotateSecret}
                    disabled={rotateSecretMutation.isPending || !!rotatedSecret}
                    title={rotatedSecret ? "Acknowledge the current secret first" : undefined}
                  >
                    {rotateSecretMutation.isPending ? "Rotating..." : "Rotate Secret"}
                  </Button>
                </div>

                {rotatedSecret && (
                  <div className="rotated-secret-display">
                    <p className="secret-warning">
                      Save this secret now - it won't be shown again!
                    </p>
                    <div className="secret-value-container">
                      <code className="secret-value">{rotatedSecret}</code>
                      <Button variant="success" size="sm" onClick={handleCopySecret}>
                        {secretCopied ? "Copied!" : "Copy"}
                      </Button>
                    </div>
                    <Button variant="ghost" size="sm" onClick={() => setRotatedSecret(null)}>
                      I've saved the secret
                    </Button>
                  </div>
                )}
              </div>

              <div className="danger-item">
                <div className="danger-item-header">
                  <div className="danger-item-info">
                    <h3>Revoke Runner</h3>
                    <p className="danger-description">
                      Revoked runners cannot reconnect. This action cannot be undone.
                    </p>
                  </div>
                  <Button
                    variant="danger"
                    onClick={handleRevoke}
                    disabled={revokeRunnerMutation.isPending}
                  >
                    {revokeRunnerMutation.isPending ? "Revoking..." : "Revoke Runner"}
                  </Button>
                </div>
              </div>
            </section>
          )}
        </div>
      </div>
    </PageShell>
  );
}
