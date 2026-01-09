import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useRunner, useUpdateRunner, useRevokeRunner, useRotateRunnerSecret } from "../hooks/useRunners";
import { useConfirm } from "../components/confirm";
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
    return date.toLocaleString();
  };

  if (isLoading) {
    return (
      <div className="runner-detail-container">
        <div>Loading runner...</div>
      </div>
    );
  }

  if (error || !runner) {
    return (
      <div className="runner-detail-container">
        <div>Runner not found</div>
        <button type="button" onClick={() => navigate("/runners")}>
          Back to Runners
        </button>
      </div>
    );
  }

  return (
    <div className="runner-detail-container">
      <div className="runner-detail-page">
        <div className="runner-detail-header">
          <button
            type="button"
            className="back-button"
            onClick={() => navigate("/runners")}
          >
            ← Back
          </button>
          <div className="runner-title-row">
            <h1>{runner.name}</h1>
            <span className={getStatusBadgeClass(runner.status)}>
              {runner.status}
            </span>
          </div>
        </div>

        {actionError && (
          <div className="action-error" role="alert">
            {actionError}
            <button type="button" className="dismiss-error" onClick={() => setActionError(null)}>
              Dismiss
            </button>
          </div>
        )}

        <div className="runner-detail-sections">
          <section className="detail-section">
            <h2>Information</h2>
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
              <h2>Capabilities</h2>
              {!isEditingCapabilities && runner.status !== "revoked" && (
                <button
                  type="button"
                  className="edit-button"
                  onClick={handleEditCapabilities}
                >
                  Edit
                </button>
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
                  <button
                    type="button"
                    className="save-button"
                    onClick={handleSaveCapabilities}
                    disabled={updateRunnerMutation.isPending}
                  >
                    {updateRunnerMutation.isPending ? "Saving..." : "Save"}
                  </button>
                  <button
                    type="button"
                    className="cancel-button"
                    onClick={() => setIsEditingCapabilities(false)}
                  >
                    Cancel
                  </button>
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
              <h2>Danger Zone</h2>

              {/* Rotate Secret */}
              <div className="danger-item">
                <div className="danger-item-header">
                  <div className="danger-item-info">
                    <h3>Rotate Secret</h3>
                    <p className="danger-description">
                      Generate a new secret, invalidating the old one. The runner will be disconnected.
                    </p>
                  </div>
                  <button
                    type="button"
                    className="rotate-secret-button"
                    onClick={handleRotateSecret}
                    disabled={rotateSecretMutation.isPending || !!rotatedSecret}
                    title={rotatedSecret ? "Acknowledge the current secret first" : undefined}
                  >
                    {rotateSecretMutation.isPending ? "Rotating..." : "Rotate Secret"}
                  </button>
                </div>

                {rotatedSecret && (
                  <div className="rotated-secret-display">
                    <p className="secret-warning">
                      Save this secret now - it won't be shown again!
                    </p>
                    <div className="secret-value-container">
                      <code className="secret-value">{rotatedSecret}</code>
                      <button
                        type="button"
                        className="copy-secret-button"
                        onClick={handleCopySecret}
                      >
                        {secretCopied ? "Copied!" : "Copy"}
                      </button>
                    </div>
                    <button
                      type="button"
                      className="dismiss-secret-button"
                      onClick={() => setRotatedSecret(null)}
                    >
                      I've saved the secret
                    </button>
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
                  <button
                    type="button"
                    className="revoke-button-large"
                    onClick={handleRevoke}
                    disabled={revokeRunnerMutation.isPending}
                  >
                    {revokeRunnerMutation.isPending ? "Revoking..." : "Revoke Runner"}
                  </button>
                </div>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
