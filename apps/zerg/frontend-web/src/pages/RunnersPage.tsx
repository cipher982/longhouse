import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useRunners, useRevokeRunner } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "../components/AddRunnerModal";
import {
  Button,
  Badge,
  Card,
  SectionHeader,
  EmptyState,
  PageShell
} from "../components/ui";
import { PlusIcon } from "../components/icons";
import { useConfirm } from "../components/confirm";
import "../styles/runners.css";

export default function RunnersPage() {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners();
  const revokeRunnerMutation = useRevokeRunner();
  const confirm = useConfirm();
  const [showAddModal, setShowAddModal] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleRevoke = async (runner: Runner) => {
    const confirmed = await confirm({
      title: `Revoke runner "${runner.name}"?`,
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
      await revokeRunnerMutation.mutateAsync(runner.id);
    } catch (err) {
      console.error("Failed to revoke runner:", err);
      setActionError("Failed to revoke runner. Please try again.");
    }
  };

  const getStatusVariant = (status: string): 'success' | 'error' | 'neutral' => {
    switch (status) {
      case "online":
        return "success";
      case "revoked":
        return "error";
      default:
        return "neutral";
    }
  };

  const formatTimestamp = (timestamp: string | null | undefined) => {
    if (!timestamp) return "Never";

    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins} min ago`;

    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours} hour${diffHours > 1 ? "s" : ""} ago`;

    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays} day${diffDays > 1 ? "s" : ""} ago`;
  };

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
          icon={<div className="spinner" style={{ width: 40, height: 40 }} />}
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
          description="Infrastructure nodes that execute commands for your agents."
          actions={
            <Button variant="primary" onClick={() => setShowAddModal(true)}>
              <PlusIcon />
              Add Runner
            </Button>
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

        {runners && runners.length === 0 ? (
          <EmptyState
            title="No runners yet"
            description="Runners let you execute commands on your own infrastructure securely."
            action={
              <Button variant="primary" size="lg" onClick={() => setShowAddModal(true)}>
                Add your first runner
              </Button>
            }
          />
        ) : (
          <div className="runners-grid">
            {runners?.map((runner) => (
              <Card
                key={runner.id}
                className="runner-card"
                onClick={() => navigate(`/runners/${runner.id}`)}
              >
                <Card.Header className="runner-card-header">
                  <h3 className="runner-card-title">{runner.name}</h3>
                  <Badge variant={getStatusVariant(runner.status)}>
                    {runner.status}
                  </Badge>
                </Card.Header>

                <Card.Body>
                  <div className="runner-card-details">
                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Platform:</span>
                      <span className="runner-detail-value">
                        {runner.runner_metadata?.platform || "Unknown"} / {runner.runner_metadata?.arch || "Unknown"}
                      </span>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Last seen:</span>
                      <span className="runner-detail-value">
                        {formatTimestamp(runner.last_seen_at)}
                      </span>
                    </div>

                    {runner.capabilities && runner.capabilities.length > 0 && (
                      <div className="runner-detail-row">
                        <span className="runner-detail-label">Capabilities:</span>
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

                  {runner.status !== "revoked" && (
                    <div className="runner-card-actions">
                      <Button
                        variant="danger"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRevoke(runner);
                        }}
                        className="runner-action-full"
                      >
                        Revoke
                      </Button>
                    </div>
                  )}
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
