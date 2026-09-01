import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useRunners } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "../components/AddRunnerModal";
import { useReadinessFlag } from "../lib/readiness-contract";
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
import {
  formatRunnerVersionValue,
  normalizeRunnerMetadata,
  runnerStatusVariant,
  updatePolicyLabel,
  versionStatusLabel,
} from "../lib/runnerPresentation";
import {
  formatHeartbeatAge,
  formatHeartbeatThreshold,
  formatVersionHint,
  getVersionVariant,
  installLayoutHint,
  installLayoutLabel,
  updatePolicyHint,
} from "../lib/runnerUtils";
import "../styles/runners.css";

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

export default function RunnersPage() {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners({ refetchInterval: 10_000 });
  const [showAddModal, setShowAddModal] = useState(false);

  // Ready signal - indicates page is interactive (even if empty)
  useReadinessFlag({ ready: !isLoading });

  if (isLoading) {
    return (
      <div className="runners-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading machines..."
          description="Fetching your connected machines."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="runners-page-container">
        <EmptyState
          variant="error"
          title="Error loading machines"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      </div>
    );
  }

  return (
    <PageShell size="wide" className="runners-page-container">
      <div className="runners-page">
        <SectionHeader
          title="Machines"
          description="Choose where Longhouse should start sessions and execute commands."
          actions={
            <Button variant="primary" data-testid="runners-add-button" onClick={() => setShowAddModal(true)}>
              <PlusIcon />
              Connect Machine
            </Button>
          }
        />

        {runners && runners.length === 0 ? (
          <EmptyState
            title="No machines connected yet"
            description="Connect a laptop, homelab box, Mac mini, or VPS so Longhouse can start sessions and run commands where your work lives."
            action={
              <Button variant="primary" size="lg" data-testid="runners-add-first-button" onClick={() => setShowAddModal(true)}>
                Connect your first machine
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
                data-testid={`runner-card-${runner.id}`}
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
                  <Badge variant={runnerStatusVariant(runner.status)}>
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
                        <span className={`runner-inline-pill runner-inline-pill--${getVersionVariant(runner.version_status)}`}>
                          {versionStatusLabel(runner.version_status)}
                        </span>
                      )}
                      {runner.capabilities_match === false && (
                        <span className="runner-inline-pill runner-inline-pill--warning">
                          capability mismatch
                        </span>
                      )}
                      {!runner.managed_install_ready && (
                        <span className="runner-inline-pill runner-inline-pill--warning">
                          legacy layout
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
                        {typeof runner.stale_after_seconds === "number" && (
                          <span className="runner-detail-subvalue">
                            {formatHeartbeatThreshold(runner.stale_after_seconds)}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Version</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">{formatRunnerVersionValue(runner)}</span>
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

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Updates</span>
                      <div className="runner-detail-stack">
                        <span className="runner-detail-value">{updatePolicyLabel(runner.auto_update_policy)}</span>
                        <span className="runner-detail-subvalue">
                          {installLayoutLabel(runner)}. {runner.managed_install_ready ? updatePolicyHint(runner.auto_update_policy) : installLayoutHint(runner)}
                        </span>
                      </div>
                    </div>

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
