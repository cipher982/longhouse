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

function platformLabel(meta: Runner["runner_metadata"]): string {
  if (!meta) return "Unknown";
  const p = (meta as Record<string, string>).platform ?? "";
  const a = (meta as Record<string, string>).arch ?? "";
  const platName = p === "darwin" ? "macOS" : p === "linux" ? "Linux" : p || "Unknown";
  return a ? `${platName} · ${a}` : platName;
}

function hostname(meta: Runner["runner_metadata"]): string | null {
  if (!meta) return null;
  return (meta as Record<string, string>).hostname ?? null;
}

export default function RunnersPage() {
  const navigate = useNavigate();
  const { data: runners, isLoading, error } = useRunners({ refetchInterval: 10_000 });
  const [showAddModal, setShowAddModal] = useState(false);

  const getStatusVariant = (status: string): 'success' | 'error' | 'neutral' => {
    switch (status) {
      case "online": return "success";
      case "revoked": return "error";
      default: return "neutral";
    }
  };

  const formatLastSeen = (timestamp: string | null | undefined) => {
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
                  <div className="runner-card-details">
                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Platform</span>
                      <span className="runner-detail-value">
                        {platformLabel(runner.runner_metadata)}
                      </span>
                    </div>

                    <div className="runner-detail-row">
                      <span className="runner-detail-label">Last seen</span>
                      <span className="runner-detail-value">
                        {formatLastSeen(runner.last_seen_at)}
                      </span>
                    </div>

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
