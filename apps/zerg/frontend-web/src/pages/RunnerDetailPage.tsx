import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useRunner, useUpdateRunner, useRevokeRunner, useRotateRunnerSecret } from "../hooks/useRunners";
import { useConfirm } from "../components/confirm";
import {
  Badge,
  Button,
  EmptyState,
  PageShell,
  SectionHeader,
  Spinner
} from "../components/ui";
import "../styles/runner-detail.css";

export default function RunnerDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const runnerId = id ? parseInt(id, 10) : 0;
  const confirm = useConfirm();

  const { data: runner, isLoading, error } = useRunner(runnerId);
  const updateRunnerMutation = useUpdateRunner();
  const revokeRunnerMutation = useRevokeRunner();
  const rotateSecretMutation = useRotateRunnerSecret();

  const [isEditingCapabilities, setIsEditingCapabilities] = useState(false);
  const [selectedCapabilities, setSelectedCapabilities] = useState<string[]>([]);
  const [rotatedSecret, setRotatedSecret] = useState<string | null>(null);
  const [secretCopied, setSecretCopied] = useState(false);
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

    // Clear any previously displayed secret before starting rotation
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

  const getStatusVariant = (status: string): "success" | "error" | "neutral" => {
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
    return date.toLocaleString();
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
          description="Manage runner capabilities, secrets, and status."
          actions={
            <div className="runner-detail-header-actions">
              <Badge variant={getStatusVariant(runner.status)}>{runner.status}</Badge>
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
              {runner.runner_metadata && (
                <>
                  <div className="detail-item">
                    <span className="detail-label">Platform:</span>
                    <span className="detail-value">{runner.runner_metadata.platform}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Architecture:</span>
                    <span className="detail-value">{runner.runner_metadata.arch}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Hostname:</span>
                    <span className="detail-value">{runner.runner_metadata.hostname}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Docker Available:</span>
                    <span className="detail-value">
                      {runner.runner_metadata.docker_available ? "Yes" : "No"}
                    </span>
                  </div>
                </>
              )}
            </div>
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

              {/* Rotate Secret */}
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

              {/* Revoke Runner */}
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
