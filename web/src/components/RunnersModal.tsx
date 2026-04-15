import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useRunners } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "./AddRunnerModal";
import { useReadinessFlag } from "../lib/readiness-contract";
import {
  Button,
  Badge,
  Card,
  EmptyState,
  Spinner,
} from "./ui";
import { PlusIcon, XIcon } from "./icons";
import { parseUTC } from "../lib/dateUtils";
import {
  formatCompactDuration,
  formatRunnerVersionValue,
  normalizeRunnerMetadata,
  runnerStatusVariant,
  updatePolicyLabel,
  versionStatusLabel,
} from "../lib/runnerPresentation";

// Re-use helpers from RunnersPage
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

function formatHeartbeatAge(runner: Runner): string {
  if (typeof runner.last_seen_age_seconds === "number") {
    return `${formatCompactDuration(runner.last_seen_age_seconds)} ago`;
  }
  if (!runner.last_seen_at) return "Never";
  const date = parseUTC(runner.last_seen_at);
  const diffMins = Math.floor((Date.now() - date.getTime()) / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  return `${Math.floor(diffHours / 24)}d ago`;
}

function versionBadgeVariant(status: string | null | undefined): "success" | "warning" | "neutral" {
  switch (status) {
    case "current": return "success";
    case "outdated": return "warning";
    default: return "neutral";
  }
}

function fallbackStatusSummary(status: string): string {
  switch (status) {
    case "online": return "Online. Live runner connection is active.";
    case "revoked": return "Revoked. This runner cannot reconnect.";
    default: return "Offline. No live runner connection is active.";
  }
}

interface RunnersModalProps {
  isOpen: boolean;
  onClose: () => void;
}

function RunnersModalContent({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners({ refetchInterval: 10_000 });
  const [showAddModal, setShowAddModal] = useState(false);

  useReadinessFlag({ ready: !isLoading });

  const handleRunnerClick = (runner: Runner) => {
    onClose();
    navigate(`/runners/${runner.id}`);
  };

  if (isLoading) {
    return (
      <div className="runners-modal-body">
        <EmptyState icon={<Spinner size="lg" />} title="Loading machines..." description="" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="runners-modal-body">
        <EmptyState variant="error" title="Error loading machines" description={error instanceof Error ? error.message : "Unknown error"} />
      </div>
    );
  }

  return (
    <div className="runners-modal-body">
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
              onClick={() => handleRunnerClick(runner)}
              data-testid={`runner-card-${runner.id}`}
            >
              <Card.Header className="runner-card-header">
                <div className="runner-card-title-group">
                  <div className="runner-card-name-row">
                    <span className={`runner-status-dot runner-status-dot--${runner.status}`} />
                    <h3 className="runner-card-title">{runner.name}</h3>
                  </div>
                  {hostname(runner.runner_metadata) && (
                    <span className="runner-card-hostname">{hostname(runner.runner_metadata)}</span>
                  )}
                </div>
                <Badge variant={runnerStatusVariant(runner.status)}>{runner.status}</Badge>
              </Card.Header>

              <Card.Body>
                <div className="runner-card-health">
                  <p className="runner-card-summary">
                    {runner.status_summary ?? fallbackStatusSummary(runner.status)}
                  </p>
                  <div className="runner-card-flags">
                    {runner.status_reason && (
                      <span className="runner-inline-pill runner-inline-pill--code">{runner.status_reason}</span>
                    )}
                    {versionStatusLabel(runner.version_status) && (
                      <span className={`runner-inline-pill runner-inline-pill--${versionBadgeVariant(runner.version_status)}`}>
                        {versionStatusLabel(runner.version_status)}
                      </span>
                    )}
                    {runner.capabilities_match === false && (
                      <span className="runner-inline-pill runner-inline-pill--warning">capability mismatch</span>
                    )}
                    {!runner.managed_install_ready && (
                      <span className="runner-inline-pill runner-inline-pill--warning">legacy layout</span>
                    )}
                  </div>
                </div>

                <div className="runner-card-details">
                  <div className="runner-detail-row">
                    <span className="runner-detail-label">Platform</span>
                    <span className="runner-detail-value">{platformLabel(runner.runner_metadata)}</span>
                  </div>
                  <div className="runner-detail-row">
                    <span className="runner-detail-label">Heartbeat</span>
                    <span className="runner-detail-value">{formatHeartbeatAge(runner)}</span>
                  </div>
                  <div className="runner-detail-row">
                    <span className="runner-detail-label">Version</span>
                    <div className="runner-detail-stack">
                      <span className="runner-detail-value">{formatRunnerVersionValue(runner)}</span>
                    </div>
                  </div>
                  <div className="runner-detail-row">
                    <span className="runner-detail-label">Updates</span>
                    <span className="runner-detail-value">{updatePolicyLabel(runner.auto_update_policy)}</span>
                  </div>
                </div>
              </Card.Body>
            </Card>
          ))}
        </div>
      )}

      {runners && runners.length > 0 && (
        <div className="runners-modal-footer">
          <Button
            variant="secondary"
            size="sm"
            data-testid="runners-add-button"
            onClick={() => setShowAddModal(true)}
          >
            <PlusIcon />
            Connect Machine
          </Button>
        </div>
      )}

      {showAddModal && (
        <AddRunnerModal isOpen onClose={() => setShowAddModal(false)} />
      )}
    </div>
  );
}

export default function RunnersModal({ isOpen, onClose }: RunnersModalProps) {
  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-container runners-modal-container"
        data-testid="runners-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>Machines</h2>
          <button
            type="button"
            className="modal-close-button"
            onClick={onClose}
            aria-label="Close"
          >
            <XIcon width={20} height={20} />
          </button>
        </div>
        <div className="modal-content">
          <RunnersModalContent onClose={onClose} />
        </div>
      </div>
    </div>
  );
}
