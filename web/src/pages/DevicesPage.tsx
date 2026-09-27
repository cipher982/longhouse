/**
 * Device Tokens Settings Page.
 *
 * Allows users to create and manage device tokens for CLI authentication.
 * Tokens are used by native `longhouse auth` to authenticate
 * with this Longhouse instance.
 */

import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  useDeviceTokens,
  useCreateDeviceToken,
  useRevokeDeviceToken,
} from "../hooks/useDeviceTokens";
import { createDeviceConnectCode, type DeviceTokenCreated } from "../services/api/devices";
import { useReadinessFlag } from "../lib/readiness-contract";
import { SectionHeader, EmptyState, Button, Badge, PageShell, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { parseUTC } from "../lib/dateUtils";
import "./DevicesPage.css";

/** One line that installs Longhouse on a server and connects it as `deviceId`. */
export function connectServerCommand(deviceId: string, token: string): string {
  const quote = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`;
  return (
    `curl -fsSL https://get.longhouse.ai/install.sh | ` +
    `LONGHOUSE_URL=${quote(window.location.origin)} LONGHOUSE_DEVICE_TOKEN=${quote(token)} ` +
    `LONGHOUSE_MACHINE_NAME=${quote(deviceId)} bash`
  );
}

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

/** How long a loopback handoff may take before the page reports it failed. */
const HANDOFF_STALL_MS = 5000;

export default function DevicesPage() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [deviceName, setDeviceName] = useState("");
  const [newToken, setNewToken] = useState<DeviceTokenCreated | null>(null);
  const [handoffStalled, setHandoffStalled] = useState(false);

  const { data, isLoading, error } = useDeviceTokens();
  const createToken = useCreateDeviceToken();
  const revokeToken = useRevokeDeviceToken();
  const confirm = useConfirm();
  // `longhouse auth` opens this page with a loopback callback, a state and a
  // PKCE challenge. A request without a challenge comes from a CLI older than
  // the code exchange; it gets told to update rather than a dead button.
  const connectRequest = (() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connect") !== "1") return null;
    const callback = params.get("callback");
    const state = params.get("state");
    const device = params.get("device");
    const challenge = params.get("challenge");
    if (!callback || !state || !device) return null;
    try {
      const url = new URL(callback);
      if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/connected") return null;
      return { callback: url, state, device, challenge };
    } catch {
      return null;
    }
  })();

  // The CLI answers the loopback visit with a 303 back to this page, carrying
  // only whether its redeem succeeded.
  const connectOutcome = (() => {
    const value = new URLSearchParams(window.location.search).get("connected");
    if (value === "1") return "ok";
    if (value === "0") return "failed";
    return null;
  })();

  // Ready signal for tests
  useReadinessFlag({ ready: !isLoading });

  // Approve the waiting CLI. The browser never holds a device token: the
  // Runtime Host returns a one-time code bound to the CLI's PKCE challenge,
  // and the page hands that code to the loopback listener as a top-level GET
  // navigation. The CLI redeems code + verifier for the token itself, so
  // nothing is minted unless the process that started the flow collects it.
  //
  // Why a navigation: this origin's CSP (`form-action 'self'`) blocks a form
  // POST to the loopback in every engine, and fetch/XHR to http://127.0.0.1
  // from https is blocked as mixed content (WebKit) or by private-network
  // access (Chromium). A top-level GET is exempt from both. The code in that
  // URL is single-use, expires in minutes and is worthless without the
  // verifier, and the listener's 303 replaces the entry anyway.
  const connectDevice = useMutation({
    mutationFn: (request: NonNullable<typeof connectRequest>) =>
      createDeviceConnectCode({ device_id: request.device, code_challenge: request.challenge ?? "" }),
    onSuccess: ({ code }, request) => {
      const target = new URL(request.callback);
      target.search = new URLSearchParams({ state: request.state, code }).toString();
      window.location.replace(target.toString());
      // A successful handoff unloads this page. Still here means the browser
      // could not reach the CLI (it exited, or the port is gone), and some
      // engines fail that navigation without showing any error page.
      window.setTimeout(() => setHandoffStalled(true), HANDOFF_STALL_MS);
    },
  });

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

      {connectOutcome && (
        <div className="token-reveal" role="status">
          <h4>{connectOutcome === "ok" ? "Device connected" : "Device connection failed"}</h4>
          <p className="token-reveal-hint">
            {connectOutcome === "ok"
              ? "The Longhouse CLI on that device holds its own token now. You can close this tab."
              : "The device could not finish connecting. Check the terminal where you ran longhouse auth, then run it again."}
          </p>
        </div>
      )}

      {connectRequest && !connectRequest.challenge && (
        <div className="token-reveal" role="alert">
          <h4>Update Longhouse on {connectRequest.device}</h4>
          <p className="token-reveal-hint">
            That Longhouse CLI is too old to connect through this page. Update it, then run longhouse auth again.
          </p>
        </div>
      )}

      {connectRequest?.challenge && (
        <div className="token-reveal">
          <h4>Connect {connectRequest.device}</h4>
          <p className="token-reveal-hint">Approve this browser request to authorize the native Longhouse client on that device.</p>
          <Button
            variant="primary"
            onClick={() => connectDevice.mutate(connectRequest)}
            disabled={connectDevice.isPending || connectDevice.isSuccess}
          >
            {handoffStalled
              ? "Not connected"
              : connectDevice.isPending || connectDevice.isSuccess
                ? "Connecting…"
                : "Connect this device"}
          </Button>
          {handoffStalled && (
            <p className="token-reveal-hint" role="alert">
              This browser could not reach longhouse auth on {connectRequest.device}. Make sure it is still running in
              your terminal; if it exited, run it again. No token was created.
            </p>
          )}
          {connectDevice.isError && (
            <p className="token-reveal-hint" role="alert">
              Could not approve this device: {connectDevice.error.message}. Try again, or run longhouse auth again.
            </p>
          )}
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
            Copy this token now — it won't be shown again.
          </p>
          {/* A server has no browser for `longhouse auth` approval, so hand it
              one line that installs, connects, and starts the Machine Agent. */}
          <p className="token-reveal-hint">To connect a Linux server or VPS, run this one line on it:</p>
          <div className="token-reveal-value" data-testid="connect-server-command">
            <code>{connectServerCommand(newToken.device_id, newToken.token)}</code>
            <Button variant="secondary" size="sm" onClick={() => handleCopy(connectServerCommand(newToken.device_id, newToken.token))}>
              Copy
            </Button>
          </div>
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
