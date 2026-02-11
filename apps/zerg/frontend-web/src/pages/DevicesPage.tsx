/**
 * Device Tokens Settings Page.
 *
 * Allows users to create and manage device tokens for CLI authentication.
 * Tokens are used by `longhouse auth` / `longhouse ship` to authenticate
 * with this Longhouse instance.
 */

import { useState, useEffect, type FormEvent } from "react";
import {
  useDeviceTokens,
  useCreateDeviceToken,
  useRevokeDeviceToken,
} from "../hooks/useDeviceTokens";
import type { DeviceTokenCreated } from "../services/api/devices";
import { SectionHeader, EmptyState, Button, Badge, PageShell, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import "./DevicesPage.css";

function formatDate(iso: string | null): string {
  if (!iso) return "Never";
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString();
}

export default function DevicesPage() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [deviceName, setDeviceName] = useState("");
  const [newToken, setNewToken] = useState<DeviceTokenCreated | null>(null);

  const { data, isLoading, error } = useDeviceTokens();
  const createToken = useCreateDeviceToken();
  const revokeToken = useRevokeDeviceToken();
  const confirm = useConfirm();

  // Ready signal for tests
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [isLoading]);

  const handleCreate = (e: FormEvent) => {
    e.preventDefault();
    if (!deviceName.trim()) return;

    createToken.mutate(
      { device_id: deviceName.trim() },
      {
        onSuccess: (created) => {
          setNewToken(created);
          setShowCreateModal(false);
          setDeviceName("");
        },
      }
    );
  };

  const handleRevoke = async (tokenId: string, deviceId: string) => {
    const confirmed = await confirm({
      title: `Revoke token for "${deviceId}"?`,
      message: "This device will no longer be able to authenticate. This cannot be undone.",
      confirmLabel: "Revoke",
      cancelLabel: "Keep",
      variant: "danger",
    });

    if (!confirmed) return;
    revokeToken.mutate(tokenId);
  };

  const handleCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      // Brief visual feedback — no toast needed for copy
    } catch {
      // Fallback: select the text
    }
  };

  if (error) {
    return (
      <PageShell size="narrow" className="devices-page-container">
        <EmptyState variant="error" title="Error loading device tokens" description={String(error)} />
      </PageShell>
    );
  }

  const tokens = data?.tokens ?? [];

  return (
    <PageShell size="narrow" className="devices-page-container">
      <SectionHeader
        title="Device Tokens"
        description="Manage tokens that authenticate CLI tools with this Longhouse instance."
      />

      {/* Newly created token — shown once */}
      {newToken && (
        <div className="token-reveal">
          <div className="token-reveal-header">
            <h4>Token created for {newToken.device_id}</h4>
            <Button variant="ghost" size="sm" onClick={() => setNewToken(null)}>
              Dismiss
            </Button>
          </div>
          <div className="token-reveal-value">
            <code>{newToken.token}</code>
            <Button variant="secondary" size="sm" onClick={() => handleCopy(newToken.token)}>
              Copy
            </Button>
          </div>
          <p className="token-reveal-hint">
            Copy this token now — it won't be shown again. Use it with:
          </p>
          <p className="token-reveal-hint">
            <code>longhouse auth --token {newToken.token}</code>
          </p>
        </div>
      )}

      <div className="devices-section">
        <div className="devices-toolbar">
          <Button variant="primary" onClick={() => setShowCreateModal(true)}>
            + Create Token
          </Button>
        </div>

        {isLoading ? (
          <EmptyState
            icon={<Spinner size="lg" />}
            title="Loading tokens..."
            description="Fetching your device tokens."
          />
        ) : tokens.length > 0 ? (
          <table className="devices-table">
            <thead>
              <tr>
                <th>Device</th>
                <th>Created</th>
                <th>Last Used</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <tr key={token.id}>
                  <td className="device-name">{token.device_id}</td>
                  <td className="device-date">{formatDate(token.created_at)}</td>
                  <td className="device-date">{formatDate(token.last_used_at)}</td>
                  <td>
                    {token.is_valid ? (
                      <Badge variant="success">Active</Badge>
                    ) : (
                      <Badge variant="error">Revoked</Badge>
                    )}
                  </td>
                  <td className="device-actions">
                    {token.is_valid && (
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleRevoke(token.id, token.device_id)}
                      >
                        Revoke
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState
            title="No device tokens"
            description="Create a token to connect CLI tools like `longhouse ship` to this instance."
          />
        )}
      </div>

      {/* CLI setup instructions */}
      <div className="cli-instructions">
        <h4>CLI Setup</h4>
        <code>{`pip install longhouse\nlonghouse auth --url ${window.location.origin}`}</code>
      </div>

      {/* Create modal */}
      {showCreateModal && (
        <div className="devices-modal-overlay" onClick={() => setShowCreateModal(false)}>
          <div className="devices-modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="devices-modal-header">
              <h3>Create Device Token</h3>
              <button className="devices-modal-close" onClick={() => setShowCreateModal(false)}>
                &times;
              </button>
            </div>
            <form onSubmit={handleCreate}>
              <div className="devices-modal-body">
                <div className="devices-form-group">
                  <label htmlFor="device-name">Device Name</label>
                  <input
                    id="device-name"
                    type="text"
                    value={deviceName}
                    onChange={(e) => setDeviceName(e.target.value)}
                    placeholder="e.g., macbook-pro, work-laptop"
                    required
                    maxLength={255}
                    autoFocus
                  />
                  <p className="devices-form-hint">
                    A label to identify which device is using this token.
                  </p>
                </div>
              </div>
              <div className="devices-modal-footer">
                <Button type="button" variant="ghost" onClick={() => setShowCreateModal(false)}>
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={createToken.isPending}
                >
                  {createToken.isPending ? "Creating..." : "Create Token"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </PageShell>
  );
}
