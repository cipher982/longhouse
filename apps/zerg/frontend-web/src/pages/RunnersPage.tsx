import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useRunners, useRevokeRunner } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "../components/AddRunnerModal";
import {
  Button,
  Badge,
  Card,
  SectionHeader,
  EmptyState
} from "../components/ui";
import { PlusIcon } from "../components/icons";
import "../styles/runners.css";

export default function RunnersPage() {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners();
  const revokeRunnerMutation = useRevokeRunner();
  const [showAddModal, setShowAddModal] = useState(false);

  const handleRevoke = async (runner: Runner) => {
    if (!confirm(`Revoke runner "${runner.name}"? It will no longer be able to connect.`)) {
      return;
    }

    try {
      await revokeRunnerMutation.mutateAsync(runner.id);
    } catch (error) {
      console.error("Failed to revoke runner:", error);
      alert("Failed to revoke runner");
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
    <div className="runners-page-container">
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
                <Card.Header>
                  <h3 style={{ margin: 0, fontSize: '1.1rem' }}>{runner.name}</h3>
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
                    <div className="runner-card-actions" style={{ marginTop: 'var(--space-4)', paddingTop: 'var(--space-4)', borderTop: '1px solid var(--border-glass-1)' }}>
                      <Button
                        variant="danger"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRevoke(runner);
                        }}
                        style={{ width: '100%' }}
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
    </div>
  );
}
