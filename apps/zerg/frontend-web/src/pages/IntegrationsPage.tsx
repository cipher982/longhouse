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
import { ConnectorCard, isOAuthConnector } from "../components/connectors/ConnectorCard";
import { useOAuthFlow } from "../hooks/useOAuthFlow";

export default function IntegrationsPage() {
  const { data: connectors, isLoading, error, refetch } = useAccountConnectors();
  const configureConnector = useConfigureAccountConnector();
  const deleteConnector = useDeleteAccountConnector();
  const testConnector = useTestAccountConnector();
  const testBeforeSave = useTestAccountConnectorBeforeSave();

  const { startOAuthFlow, oauthPending } = useOAuthFlow(refetch);

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
  const infrastructure = connectors?.filter((c) => c.category === "infrastructure") ?? [];

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
          </p>
          <div className="integrations-info-box">
            <div className="info-box-icon">💡</div>
            <div className="info-box-content">
              <strong>Credential Hierarchy</strong>
              <p>
                Account-level credentials configured here are used by all your agents by default.
                You can override specific credentials per-agent in the agent's settings if needed
                (e.g., different GitHub tokens for personal vs. work projects).
              </p>
            </div>
          </div>
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

            <div className="connector-group">
              <h3>Project Management</h3>
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

            {infrastructure.length > 0 && (
              <div className="connector-group">
                <h3>Infrastructure</h3>
                <p className="section-description">
                  Configure SSH targets for remote command execution via runners.
                  Runners use your local SSH config to access these targets.
                </p>
                <div className="connector-cards">
                  {infrastructure.map((connector) => (
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
            )}
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
