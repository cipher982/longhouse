/**
 * Account-level Integrations Settings Page.
 *
 * Allows users to configure connector credentials at the account level.
 * These credentials are shared across all agents owned by the user.
 */

import { useState, type FormEvent } from "react";
import {
  useAccountConnectors,
  useConfigureAccountConnector,
  useDeleteAccountConnector,
  useTestAccountConnector,
  useTestAccountConnectorBeforeSave,
} from "../hooks/useAccountConnectors";
import type { AccountConnectorStatus } from "../types/connectors";
import { ConnectorConfigModal, type ConfigModalState } from "../components/agent-settings/ConnectorConfigModal";

export default function IntegrationsPage() {
  const { data: connectors, isLoading, error } = useAccountConnectors();
  const configureConnector = useConfigureAccountConnector();
  const deleteConnector = useDeleteAccountConnector();
  const testConnector = useTestAccountConnector();
  const testBeforeSave = useTestAccountConnectorBeforeSave();

  const [modal, setModal] = useState<ConfigModalState>({
    isOpen: false,
    connector: null,
    credentials: {},
    displayName: "",
  });

  const openConfigModal = (connector: AccountConnectorStatus) => {
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

  const handleDelete = (connector: AccountConnectorStatus) => {
    if (!window.confirm(`Remove ${connector.name} integration from your account?`)) {
      return;
    }
    deleteConnector.mutate(connector.type);
  };

  const handleTest = (connector: AccountConnectorStatus) => {
    testConnector.mutate(connector.type);
  };

  // Group connectors by category
  const notifications = connectors?.filter((c) => c.category === "notifications") ?? [];
  const projectManagement = connectors?.filter((c) => c.category === "project_management") ?? [];

  if (error) {
    return (
      <div className="integrations-container">
        <div className="integrations-content">
          <h2>Integrations</h2>
          <p className="error-message">Failed to load integrations: {String(error)}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="integrations-container">
      <div className="integrations-content">
        <div className="integrations-header">
          <h2>Integrations</h2>
          <p className="integrations-description">
            Configure credentials for external services. These integrations are shared across all your agents.
            Individual agents can override these settings if needed.
          </p>
        </div>

        {isLoading ? (
          <p className="muted">Loading integrations…</p>
        ) : (
          <div className="connector-groups">
            <div className="connector-group">
              <h3>Notifications</h3>
              <p className="section-description">
                Configure webhooks and API keys for notification tools (Slack, Discord, Email, SMS).
              </p>
              <div className="connector-cards">
                {notifications.map((connector) => (
                  <IntegrationCard
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
              <h3>Project Management</h3>
              <p className="section-description">
                Configure API tokens for project management tools (GitHub, Jira, Linear, Notion).
              </p>
              <div className="connector-cards">
                {projectManagement.map((connector) => (
                  <IntegrationCard
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
          </div>
        )}
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
    </div>
  );
}

type IntegrationCardProps = {
  connector: AccountConnectorStatus;
  onConfigure: () => void;
  onTest: () => void;
  onDelete: () => void;
  isTesting: boolean;
};

function IntegrationCard({ connector, onConfigure, onTest, onDelete, isTesting }: IntegrationCardProps) {
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
            .filter(([k]) => !["enabled", "from_email", "from_number"].includes(k))
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
            <button type="button" className="btn-secondary" onClick={onConfigure}>
              Edit
            </button>
            <button type="button" className="btn-tertiary" onClick={onTest} disabled={isTesting}>
              Test
            </button>
            <button type="button" className="btn-danger" onClick={onDelete}>
              Remove
            </button>
          </>
        ) : (
          <button type="button" className="btn-primary" onClick={onConfigure}>
            Configure
          </button>
        )}
      </div>
    </div>
  );
}

