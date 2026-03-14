import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useRunners } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "../components/AddRunnerModal";
import {
  Button,
  Badge,
  Card,
  SectionHeader,
  EmptyState,
  PageShell,
  Spinner
} from "../components/ui";
import { PlusIcon } from "../components/icons";
import { parseUTC } from "../lib/dateUtils";
import "../styles/runners.css";

type RunnerMetadataSummary = {
  platform?: string;
  arch?: string;
  hostname?: string;
};

function normalizeRunnerMetadata(metadata: unknown): RunnerMetadataSummary | null {
  if (!metadata || typeof metadata !== "object") {
    return null;
  }

  const record = metadata as Record<string, unknown>;
  return {
    platform: typeof record.platform === "string" ? record.platform : undefined,
    arch: typeof record.arch === "string" ? record.arch : undefined,
    hostname: typeof record.hostname === "string" ? record.hostname : undefined,
  };
}

function platformLabel(meta: Runner["runner_metadata"]): string {
  const metadata = normalizeRunnerMetadata(meta);
  if (!metadata) return "Unknown";

  const p = metadata.platform ?? "";
  const a = metadata.arch ?? "";
  const platName = p === "darwin" ? "macOS" : p === "linux" ? "Linux" : p || "Unknown";
  return a ? `${platName} · ${a}` : platName;
}

function hostname(meta: Runner["runner_metadata"]): string | null {
  return normalizeRunnerMetadata(meta)?.hostname ?? null;
}

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

function formatLastSeen(timestamp: string | null | undefined) {
  if (!timestamp) return "Never";
  const date = parseUTC(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  return `${Math.floor(diffHours / 24)}d ago`;
}

function formatHeartbeatAge(runner: Runner): string {
  if (typeof runner.last_seen_age_seconds === "number") {
    return `${formatCompactDuration(runner.last_seen_age_seconds)} ago`;
  }
  return formatLastSeen(runner.last_seen_at);
}

function formatStaleThreshold(staleAfterSeconds: number | null | undefined): string | null {
  if (typeof staleAfterSeconds !== "number") {
    return null;
  }
  return `Stale after ${formatCompactDuration(staleAfterSeconds)}`;
}

function fallbackStatusSummary(status: string): string {
  switch (status) {
    case "online":
      return "Online. Live runner connection is active.";
    case "revoked":
      return "Revoked. This runner cannot reconnect.";
    default:
      return "Offline. No live runner connection is active.";
  }
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

function versionBadgeVariant(status: string | null | undefined): "success" | "warning" | "neutral" {
  switch (status) {
    case "current":
      return "success";
    case "outdated":
      return "warning";
    default:
      return "neutral";
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
      return "Matches the latest published runner build.";
    case "outdated":
      return runner.latest_runner_version
        ? `Latest expected version is v${runner.latest_runner_version}.`
        : "This runner is behind the expected version.";
    case "ahead":
      return runner.latest_runner_version
        ? `Runner is newer than configured latest v${runner.latest_runner_version}.`
        : "Runner version is ahead of the configured latest build.";
    default:
      return null;
  }
}

export default function RunnersPage() {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners({ refetchInterval: 10_000 });
  const [showAddModal, setShowAddModal] = useState(false);

  // Ready signal - indicates page is interactive (even if empty)
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute('data-ready', 'true');
    }
    return () => document.body.removeAttribute('data-ready');
  }, [isLoading]);

  if (isLoading) {
    return (
      <div className="runners-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading runners..."
          description="Fetching your connected infrastructure."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="runners-page-container">
        <EmptyState
          variant="error"
          title="Error loading runners"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      </div>
    );
  }

  return (
    <PageShell size="wide" className="runners-page-container">
      <div className="runners-page">
        <SectionHeader
          title="Runners"
          description="Infrastructure nodes that execute commands for your fiches."
          actions={
            <Button variant="primary" data-testid="runners-add-button" onClick={() => setShowAddModal(true)}>
              <PlusIcon />
              Add Runner
            </Button>
          }
        />

        {runners && runners.length === 0 ? (
          <EmptyState
            title="No runners yet"
            description="Runners let you execute commands on your own infrastructure securely."
            action={
              <Button variant="primary" size="lg" data-testid="runners-add-first-button" onClick={() => setShowAddModal(true)}>
                Add your first runner
              </Button>
            }
          />
        ) : (
          <div className="runners-grid">
            {runners?.map((runner) => (
              <Card
                key={runner.id}
                className={`runner-card runner-card--${runner.status}`}
                onClick={() => navigate(`/runners/${runner.id}`)}
              >
                <Card.Header className="runner-card-header">
                  <div className="runner-card-title-group">
                    <div className="runner-card-name-row">
                      <span className={`runner-status-dot runner-status-dot--${runner.status}`} />
                      <h3 className="runner-card-title">{runner.name}</h3>
                    </div>
                    {hostname(runner.runner_metadata) && (
                      <span className="runner-card-hostname">
                        {hostname(runner.runner_metadata)}
                      </span>
                    )}
                  </div>
                  <Badge variant={getStatusVariant(runner.status)}>
                    {runner.status}
                  </Badge>
                </Card.Header>

                <Card.Body>
                  <div className="runner-card-health">
                    <p className="runner-card-summary">
                      {runner.status_summary ?? fallbackStatusSummary(runner.status)}
                    </p>
                    <div className="runner-card-flags">
                      {runner.status_reason && (
                        <span className="runner-inline-pill runner-inline-pill--code">
                          {runner.status_reason}
                        </span>
                      )}
                      {versionStatusLabel(runner.version_status) && (
                        <span className={`runner-inline-pill runner-inline-pill--${versionBadgeVariant(runner.version_status)}`}>
                          {versionStatusLabel(runner.version_status)}
                        </span>
                      )}
                      {runner.capabilities_match === false && (
                        <span className="runner-inline-pill runner-inline-pill--warning">
                          capability mismatch
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="runner-card-details">
                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Platform</span>
                      <span className="runner-detail-value">
                        {platformLabel(runner.runner_metadata)}
                      </span>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Heartbeat</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">
                          {formatHeartbeatAge(runner)}
                        </span>
                        {formatStaleThreshold(runner.stale_after_seconds) && (
                          <span className="runner-detail-subvalue">
                            {formatStaleThreshold(runner.stale_after_seconds)}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Version</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">{formatVersionValue(runner)}</span>
                        {formatVersionHint(runner) && (
                          <span className="runner-detail-subvalue">{formatVersionHint(runner)}</span>
                        )}
                      </div>
                    </div>

                    {runner.install_mode && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Install</span>
                        <span className="runner-detail-value">{runner.install_mode}</span>
                      </div>
                    )}

                    {runner.reported_capabilities && runner.capabilities_match === false && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Runner reported</span>
                        <span className="runner-detail-value runner-detail-value--inline-list">
                          {runner.reported_capabilities.join(", ")}
                        </span>
                      </div>
                    )}

                    {runner.capabilities && runner.capabilities.length > 0 && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Capabilities</span>
                        <div className="capabilities-list">
                          {runner.capabilities.map((cap) => (
                            <span key={cap} className="capability-chip">
                              {cap}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </Card.Body>
              </Card>
            ))}
          </div>
        )}
      </div>

      {showAddModal && (
        <AddRunnerModal
          isOpen={showAddModal}
          onClose={() => setShowAddModal(false)}
        />
      )}
    </PageShell>
  );
}
