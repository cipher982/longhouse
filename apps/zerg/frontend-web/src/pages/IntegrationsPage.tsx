/**
 * Account-level Integrations Settings Page.
 *
 * Allows users to configure connector credentials at the account level.
 * These credentials are shared across all agents owned by the user.
 */

import { useState, useEffect, type FormEvent } from "react";
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
import { SectionHeader, EmptyState } from "../components/ui";
import { useConfirm } from "../components/confirm";

export default function IntegrationsPage() {
  const { data: connectors, isLoading, error, refetch } = useAccountConnectors();
  const configureConnector = useConfigureAccountConnector();
  const deleteConnector = useDeleteAccountConnector();
  const testConnector = useTestAccountConnector();
  const testBeforeSave = useTestAccountConnectorBeforeSave();
  const confirm = useConfirm();

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

  const handleDelete = async (connector: AccountConnectorStatus) => {
    const confirmed = await confirm({
      title: `Remove ${connector.name}?`,
      message: 'This will remove this integration from your account. Any agents using these credentials will lose access.',
      confirmLabel: 'Remove',
      cancelLabel: 'Keep',
      variant: 'danger',
    });
    if (!confirmed) {
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

  // Ready signal - indicates page is interactive (even if empty)
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute('data-ready', 'true');
    }
    return () => document.body.removeAttribute('data-ready');
  }, [isLoading]);

  if (error) {
    return (
      <div className="integrations-page-container">
        <EmptyState
          variant="error"
          title="Error loading integrations"
          description={String(error)}
        />
      </div>
    );
  }

  return (
    <div className="integrations-page-container">
      <SectionHeader
        title="Integrations"
        description="Configure credentials for external services. These integrations are shared across all your agents."
      />

      <div className="integrations-content">
        {isLoading ? (
          <EmptyState
            icon={<div className="spinner" style={{ width: 40, height: 40 }} />}
            title="Loading integrations..."
            description="Fetching your connected services."
          />
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
