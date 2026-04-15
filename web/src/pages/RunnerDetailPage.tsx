import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useCreateEnrollToken,
  useDeleteRunner,
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
import {
  buildRunnerNativeInstallCommand,
  describeRunnerNativeInstallMode,
  type RunnerNativeInstallMode,
} from "../lib/runnerInstallCommands";
import {
  formatCompactDuration,
  formatRunnerVersionValue,
  normalizeRunnerMetadata,
  runnerStatusVariant as getStatusVariant,
  updatePolicyLabel,
  versionStatusLabel,
} from "../lib/runnerPresentation";
import {
  capabilitySyncHint,
  capabilitySyncLabel,
  defaultRepairMode,
  formatHeartbeatAge,
  formatHeartbeatInterval,
  formatHeartbeatThreshold,
  formatRelativeTimestamp,
  formatTimestamp,
  formatVersionHint,
  getJobStatusVariant,
  getVersionVariant,
  installLayoutHint,
  installLayoutLabel,
  jobDuration,
  jobPreview,
  updatePolicyHint,
} from "../lib/runnerUtils";
import type { RunnerDoctorResponse } from "../services/api";
import "../styles/runner-detail.css";

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
  const deleteRunnerMutation = useDeleteRunner();
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

  const handleDelete = async () => {
    const confirmed = await confirm({
      title: `Forget machine "${runner?.name}"?`,
      message:
        "This permanently removes the machine from Longhouse. Existing sessions stay, but this machine's runner jobs and health incidents are deleted.",
      confirmLabel: "Forget Machine",
      cancelLabel: "Keep",
      variant: "danger",
    });
    if (!confirmed) {
      return;
    }

    setActionError(null);
    try {
      await deleteRunnerMutation.mutateAsync(runnerId);
      navigate("/runners");
    } catch (err) {
      console.error("Failed to delete runner:", err);
      setActionError("Failed to forget this machine. Please try again.");
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
          title="Loading machine..."
          description="Fetching machine details."
        />
      </PageShell>
    );
  }

  if (error || !runner) {
    return (
      <PageShell size="wide" className="runner-detail-container">
        <EmptyState
          variant="error"
          title="Machine not found"
          description="This machine might have been disconnected, revoked, or removed."
          action={
            <Button variant="secondary" onClick={() => navigate("/runners")}>
              Back to Machines
            </Button>
          }
        />
      </PageShell>
    );
  }

  const canForgetRunner = runner.status !== "online";

  return (
    <PageShell size="wide" className="runner-detail-container">
      <div className="runner-detail-page">
        <SectionHeader
          title={runner.name}
          description={runner.status_summary ?? "Manage this machine's connection, capabilities, secrets, and status."}
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
                <p className="detail-help-text">Derived from machine heartbeats, reported metadata, and Longhouse policy.</p>
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
                {!runner.managed_install_ready && (
                  <Badge variant="warning">legacy layout</Badge>
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
                <span className="detail-label">Update Policy</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{updatePolicyLabel(runner.auto_update_policy)}</span>
                  <span className="detail-subvalue">{updatePolicyHint(runner.auto_update_policy)}</span>
                </div>
              </div>
              <div className="detail-item">
                <span className="detail-label">Update Layout</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{installLayoutLabel(runner)}</span>
                  <span className="detail-subvalue">{installLayoutHint(runner)}</span>
                </div>
              </div>
              <div className="detail-item">
                <span className="detail-label">Version</span>
                <div className="detail-value-stack">
                  <span className="detail-value">{formatRunnerVersionValue(runner)}</span>
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

          {(runner.status !== "revoked" || canForgetRunner) && (
            <section className="detail-section danger-section">
              <h2 className="ui-section-title">Danger Zone</h2>

              {runner.status !== "revoked" && (
                <>
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
                </>
              )}

              {canForgetRunner && (
                <div className="danger-item">
                  <div className="danger-item-header">
                    <div className="danger-item-info">
                      <h3>Forget Machine</h3>
                      <p className="danger-description">
                        Permanently remove this stale machine from Longhouse. Existing sessions stay, but runner jobs and health incidents for this machine are deleted.
                      </p>
                    </div>
                    <Button
                      variant="danger"
                      onClick={handleDelete}
                      disabled={deleteRunnerMutation.isPending}
                    >
                      {deleteRunnerMutation.isPending ? "Forgetting..." : "Forget Machine"}
                    </Button>
                  </div>
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </PageShell>
  );
}
