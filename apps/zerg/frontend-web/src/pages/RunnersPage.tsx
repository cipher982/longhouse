import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useRunners, useRevokeRunner } from "../hooks/useRunners";
import type { Runner } from "../services/api";
import AddRunnerModal from "../components/AddRunnerModal";
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

  const getStatusBadgeClass = (status: string) => {
    switch (status) {
      case "online":
        return "status-badge status-online";
      case "revoked":
        return "status-badge status-revoked";
      default:
        return "status-badge status-offline";
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
      <div className="runners-container">
        <div className="runners-page">
          <div>Loading runners...</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="runners-container">
        <div className="runners-page">
          <div>Error loading runners: {error instanceof Error ? error.message : "Unknown error"}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="runners-container">
      <div className="runners-page">
        <div className="runners-header">
          <h1>Runners</h1>
          <button
            type="button"
            className="add-runner-button"
            onClick={() => setShowAddModal(true)}
          >
            Add Runner
          </button>
        </div>

        {runners && runners.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-content">
              <h2>No runners yet</h2>
              <p>Runners let you execute commands on your own infrastructure securely.</p>
              <button
                type="button"
                className="add-runner-button-large"
                onClick={() => setShowAddModal(true)}
              >
                Add your first runner
              </button>
            </div>
          </div>
        ) : (
          <div className="runners-grid">
            {runners?.map((runner) => (
              <div
                key={runner.id}
                className="runner-card"
                onClick={() => navigate(`/runners/${runner.id}`)}
              >
                <div className="runner-card-header">
                  <h3>{runner.name}</h3>
                  <span className={getStatusBadgeClass(runner.status)}>
                    {runner.status}
                  </span>
                </div>

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
                    <button
                      type="button"
                      className="runner-action-button revoke-button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleRevoke(runner);
                      }}
                    >
                      Revoke
                    </button>
                  </div>
                )}
              </div>
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
