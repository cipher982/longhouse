/**
 * Device Tokens Settings Page.
 *
 * Allows users to create and manage device tokens for CLI authentication.
 * Tokens are used by native `longhouse auth` to authenticate
 * with this Longhouse instance.
 */

import { useState, type FormEvent } from "react";
import {
  useDeviceTokens,
  useCreateDeviceToken,
  useRevokeDeviceToken,
} from "../hooks/useDeviceTokens";
import type { DeviceTokenCreated } from "../services/api/devices";
import { useReadinessFlag } from "../lib/readiness-contract";
import { SectionHeader, EmptyState, Button, Badge, PageShell, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { parseUTC } from "../lib/dateUtils";
import "./DevicesPage.css";

function formatDate(iso: string | null): string {
  if (!iso) return "Never";
  const d = parseUTC(iso);
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
  const connectRequest = (() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connect") !== "1") return null;
    const callback = params.get("callback");
    const state = params.get("state");
    const device = params.get("device");
    if (!callback || !state || !device) return null;
    try {
      const url = new URL(callback);
      if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/connected") return null;
      return { callback: url, state, device };
    } catch {
      return null;
    }
  })();

  // The CLI's callback answers 303 back to this page, so the request that
  // carried the token is never left as the current history entry. `connected`
  // is the only trace of it, and it carries no credential.
  const connectOutcome = (() => {
    const value = new URLSearchParams(window.location.search).get("connected");
    if (value === "1") return "ok";
    if (value === "0") return "failed";
    return null;
  })();

  // Ready signal for tests
  useReadinessFlag({ ready: !isLoading });

  // Hand the token to the waiting CLI as a top-level form POST. Two constraints
  // pin that shape, and neither is a style choice:
  //   * The token must not ride in a URL. Device tokens never expire and
  //     authorize the whole agents surface, so a query-string callback writes a
  //     live credential into browser history permanently.
  //   * It cannot be fetch/XHR. A subresource request from https://longhouse.ai
  //     to http://127.0.0.1 triggers a private-network preflight that the raw
  //     TcpListener in `browser_device_token` (engine/src/longhouse.rs) cannot
  //     answer. Top-level navigations are exempt from that preflight, and
  //     127.0.0.1 is a potentially-trustworthy origin, so the submission is not
  //     treated as mixed content either.
  // The listener reads the request line, the headers and Content-Length bytes
  // of body, then answers 303 back to this page — so the callback URL never
  // survives as a history entry at all.
  const deliverToken = (token: string) => {
    if (!connectRequest) return;
    const form = document.createElement("form");
    form.method = "POST";
    form.action = connectRequest.callback.toString();
    form.enctype = "application/x-www-form-urlencoded";
    form.hidden = true;
    for (const [name, value] of [
      ["state", connectRequest.state],
      ["token", token],
    ]) {
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = name;
      field.value = value;
      form.appendChild(field);
    }
    document.body.appendChild(form);
    form.submit();
  };

  const handleCreate = (e: FormEvent) => {
    e.preventDefault();
    if (!deviceName.trim()) return;

    createToken.mutate(
      { device_id: deviceName.trim() },
      {
        onSuccess: (created) => {
          if (connectRequest) {
            deliverToken(created.token);
            return;
          }
          setNewToken(created);
          setShowCreateModal(false);
          setDeviceName("");
        },
      }
    );
  };

  const handleConnect = () => {
    if (!connectRequest) return;
    createToken.mutate(
      { device_id: connectRequest.device },
      {
        onSuccess: (created) => {
          deliverToken(created.token);
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

      {connectOutcome && (
        <div className="token-reveal">
          <h4>{connectOutcome === "ok" ? "Device connected" : "Device connection failed"}</h4>
          <p className="token-reveal-hint">
            {connectOutcome === "ok"
              ? "The native client on that device holds its own token now."
              : "The device did not accept the token. Run longhouse auth again on that device."}
          </p>
        </div>
      )}

      {connectRequest && (
        <div className="token-reveal">
          <h4>Connect {connectRequest.device}</h4>
          <p className="token-reveal-hint">Approve this browser request to authorize the native Longhouse client on that device.</p>
          <Button variant="primary" onClick={handleConnect} disabled={createToken.isPending}>
            {createToken.isPending ? "Connecting…" : "Connect this device"}
          </Button>
        </div>
      )}

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
            Copy this token now — it won't be shown again. It is for headless automation only; normal device setup uses browser approval with <code>longhouse auth --url {window.location.origin}</code>.
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
            description="Create a token to authenticate a native Longhouse device to this instance."
          />
        )}
      </div>

      {/* CLI setup instructions */}
      <div className="cli-instructions">
        <h4>CLI Setup</h4>
        <code>{`curl -fsSL https://get.longhouse.ai/install.sh | bash\nlonghouse auth --url ${window.location.origin}`}</code>
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
