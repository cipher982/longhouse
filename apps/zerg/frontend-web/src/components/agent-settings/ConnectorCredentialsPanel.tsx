/**
 * Connector Credentials Panel for Agent Settings.
 *
 * Displays all available connectors, their configuration status,
 * and provides UI for configuring/testing/removing credentials.
 */

import { useState, useEffect, useCallback, type FormEvent } from "react";
import {
  useAgentConnectors,
  useConfigureConnector,
  useDeleteConnector,
  useTestConnector,
  useTestConnectorBeforeSave,
} from "../../hooks/useAgentConnectors";
import type { ConnectorStatus } from "../../types/connectors";
import { ConnectorConfigModal, type ConfigModalState } from "./ConnectorConfigModal";

// Connectors that support OAuth flow instead of manual credential entry
const OAUTH_CONNECTORS = ["github"] as const;
type OAuthConnector = (typeof OAUTH_CONNECTORS)[number];

function isOAuthConnector(type: string): type is OAuthConnector {
  return OAUTH_CONNECTORS.includes(type as OAuthConnector);
}

type ConnectorCredentialsPanelProps = {
  agentId: number;
};

export function ConnectorCredentialsPanel({ agentId }: ConnectorCredentialsPanelProps) {
  const { data: connectors, isLoading, refetch } = useAgentConnectors(agentId);
  const configureConnector = useConfigureConnector(agentId);
  const deleteConnector = useDeleteConnector(agentId);
  const testConnector = useTestConnector(agentId);
  const testBeforeSave = useTestConnectorBeforeSave(agentId);
  const [oauthPending, setOauthPending] = useState<string | null>(null);

  const [modal, setModal] = useState<ConfigModalState>({
    isOpen: false,
    connector: null,
    credentials: {},
    displayName: "",
  });

  // Handle OAuth popup result via postMessage
  const handleOAuthMessage = useCallback(
    (event: MessageEvent) => {
      // Validate message structure
      if (!event.data || typeof event.data !== "object") return;
      const { success, provider, username, error } = event.data;

      // Only handle OAuth results
      if (provider !== oauthPending) return;

      setOauthPending(null);

      if (success) {
        // Refresh connectors list to show new connection
        refetch();
      } else if (error) {
        console.error(`OAuth connection failed: ${error}`);
        alert(`Failed to connect ${provider}: ${error}`);
      }
    },
    [oauthPending, refetch]
  );

  // Listen for OAuth popup messages
  useEffect(() => {
    window.addEventListener("message", handleOAuthMessage);
    return () => window.removeEventListener("message", handleOAuthMessage);
  }, [handleOAuthMessage]);

  // Open OAuth popup for supported connectors
  const startOAuthFlow = (connectorType: string) => {
    setOauthPending(connectorType);

    // Open popup centered on screen
    const width = 600;
    const height = 700;
    const left = window.screenX + (window.outerWidth - width) / 2;
    const top = window.screenY + (window.outerHeight - height) / 2;

    const popup = window.open(
      `/api/oauth/${connectorType}/authorize`,
      `oauth-${connectorType}`,
      `width=${width},height=${height},left=${left},top=${top},popup=1`
    );

    // Handle popup blocked or closed
    if (!popup) {
      setOauthPending(null);
      alert("Popup blocked. Please allow popups for this site.");
      return;
    }

    // Check if popup was closed without completing
    const checkClosed = setInterval(() => {
      if (popup.closed) {
        clearInterval(checkClosed);
        setOauthPending(null);
      }
    }, 500);
  };

  const openConfigModal = (connector: ConnectorStatus) => {
    const initialCreds: Record<string, string> = {};
    for (const field of connector.fields) {
      initialCreds[field.key] = "";
    }
    setModal({
      isOpen: true,
      connector,
      credentials: initialCreds,
      displayName: connector.display_name ?? "",
    });
  };

  const closeModal = () => {
    setModal({
      isOpen: false,
      connector: null,
      credentials: {},
      displayName: "",
    });
  };

  const handleCredentialChange = (key: string, value: string) => {
    setModal((prev) => ({
      ...prev,
      credentials: { ...prev.credentials, [key]: value },
    }));
  };

  const handleDisplayNameChange = (value: string) => {
    setModal((prev) => ({ ...prev, displayName: value }));
  };

  const handleTestBeforeSave = () => {
    if (!modal.connector) return;
    testBeforeSave.mutate({
      connector_type: modal.connector.type,
      credentials: modal.credentials,
    });
  };

  const handleSave = (e: FormEvent) => {
    e.preventDefault();
    if (!modal.connector) return;
    configureConnector.mutate(
      {
        connector_type: modal.connector.type,
        credentials: modal.credentials,
        display_name: modal.displayName || undefined,
      },
      {
        onSuccess: () => closeModal(),
      }
    );
  };

  const handleDelete = (connector: ConnectorStatus) => {
    if (!window.confirm(`Remove ${connector.name} credentials from this agent?`)) {
      return;
    }
    deleteConnector.mutate(connector.type);
  };

  const handleTest = (connector: ConnectorStatus) => {
    testConnector.mutate(connector.type);
  };

  // Group connectors by category
  const notifications = connectors?.filter((c) => c.category === "notifications") ?? [];
  const projectManagement = connectors?.filter((c) => c.category === "project_management") ?? [];

  if (isLoading) {
    return <p className="muted">Loading connectors…</p>;
  }

  return (
    <>
      <div className="connector-groups">
        <div className="connector-group">
          <h4>Notifications</h4>
          <p className="section-description">
            Configure webhooks and API keys for notification tools (Slack, Discord, Email, SMS).
          </p>
          <div className="connector-cards">
            {notifications.map((connector) => (
              <ConnectorCard
                key={connector.type}
                connector={connector}
                onConfigure={() => openConfigModal(connector)}
                onTest={() => handleTest(connector)}
                onDelete={() => handleDelete(connector)}
                isTesting={testConnector.isPending}
              />
            ))}
          </div>
        </div>

        <div className="connector-group">
          <h4>Project Management</h4>
          <p className="section-description">
            Configure API tokens for project management tools (GitHub, Jira, Linear, Notion).
          </p>
          <div className="connector-cards">
            {projectManagement.map((connector) => (
              <ConnectorCard
                key={connector.type}
                connector={connector}
                onConfigure={() => openConfigModal(connector)}
                onOAuthConnect={isOAuthConnector(connector.type) ? () => startOAuthFlow(connector.type) : undefined}
                onTest={() => handleTest(connector)}
                onDelete={() => handleDelete(connector)}
                isTesting={testConnector.isPending}
                isOAuthPending={oauthPending === connector.type}
              />
            ))}
          </div>
        </div>
      </div>

      <ConnectorConfigModal
        modal={modal}
        onClose={closeModal}
        onSave={handleSave}
        onTest={handleTestBeforeSave}
        onCredentialChange={handleCredentialChange}
        onDisplayNameChange={handleDisplayNameChange}
        isSaving={configureConnector.isPending}
        isTesting={testBeforeSave.isPending}
      />
    </>
  );
}

type ConnectorCardProps = {
  connector: ConnectorStatus;
  onConfigure: () => void;
  onOAuthConnect?: () => void;
  onTest: () => void;
  onDelete: () => void;
  isTesting: boolean;
  isOAuthPending?: boolean;
};

function ConnectorCard({
  connector,
  onConfigure,
  onOAuthConnect,
  onTest,
  onDelete,
  isTesting,
  isOAuthPending,
}: ConnectorCardProps) {
  const statusClass = connector.configured
    ? connector.test_status === "success"
      ? "status-success"
      : connector.test_status === "failed"
      ? "status-failed"
      : "status-untested"
    : "status-unconfigured";

  const statusText = connector.configured
    ? connector.test_status === "success"
      ? "Connected"
      : connector.test_status === "failed"
      ? "Failed"
      : "Untested"
    : "Not configured";

  // Check if connected via OAuth (metadata will have connected_via: "oauth")
  const connectedViaOAuth = connector.metadata?.connected_via === "oauth";

  return (
    <div className={`connector-card ${statusClass}`}>
      <div className="connector-card-header">
        <span className="connector-name">{connector.name}</span>
        <span className={`connector-status ${statusClass}`}>{statusText}</span>
      </div>

      {connector.configured && connector.display_name && (
        <div className="connector-display-name">{connector.display_name}</div>
      )}

      {connector.configured && connector.metadata && (
        <div className="connector-metadata">
          {Object.entries(connector.metadata)
            .filter(([k]) => !["enabled", "from_email", "from_number", "connected_via"].includes(k))
            .slice(0, 2)
            .map(([key, value]) => (
              <span key={key} className="metadata-item">
                {String(value)}
              </span>
            ))}
        </div>
      )}

      <div className="connector-card-actions">
        {connector.configured ? (
          <>
            {connectedViaOAuth && onOAuthConnect ? (
              <button
                type="button"
                className="btn-secondary"
                onClick={onOAuthConnect}
                disabled={isOAuthPending}
              >
                {isOAuthPending ? "Connecting..." : "Reconnect"}
              </button>
            ) : (
              <button type="button" className="btn-secondary" onClick={onConfigure}>
                Edit
              </button>
            )}
            <button type="button" className="btn-tertiary" onClick={onTest} disabled={isTesting}>
              Test
            </button>
            <button type="button" className="btn-danger" onClick={onDelete}>
              Remove
            </button>
          </>
        ) : onOAuthConnect ? (
          <button
            type="button"
            className="btn-primary btn-oauth"
            onClick={onOAuthConnect}
            disabled={isOAuthPending}
          >
            {isOAuthPending ? "Connecting..." : `Connect ${connector.name}`}
          </button>
        ) : (
          <button type="button" className="btn-primary" onClick={onConfigure}>
            Configure
          </button>
        )}
      </div>
    </div>
  );
}

export default ConnectorCredentialsPanel;
