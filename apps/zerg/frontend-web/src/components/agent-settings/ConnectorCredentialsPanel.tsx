/**
 * Connector Credentials Panel for Agent Settings.
 *
 * Displays all available connectors, their configuration status,
 * and provides UI for configuring/testing/removing credentials.
 */

import { useState, type FormEvent } from "react";
import {
  useAgentConnectors,
  useConfigureConnector,
  useDeleteConnector,
  useTestConnector,
  useTestConnectorBeforeSave,
} from "../../hooks/useAgentConnectors";
import type { ConnectorStatus, CredentialField } from "../../types/connectors";

type ConnectorCredentialsPanelProps = {
  agentId: number;
};

type ConfigModalState = {
  isOpen: boolean;
  connector: ConnectorStatus | null;
  credentials: Record<string, string>;
  displayName: string;
};

export function ConnectorCredentialsPanel({ agentId }: ConnectorCredentialsPanelProps) {
  const { data: connectors, isLoading } = useAgentConnectors(agentId);
  const configureConnector = useConfigureConnector(agentId);
  const deleteConnector = useDeleteConnector(agentId);
  const testConnector = useTestConnector(agentId);
  const testBeforeSave = useTestConnectorBeforeSave(agentId);

  const [modal, setModal] = useState<ConfigModalState>({
    isOpen: false,
    connector: null,
    credentials: {},
    displayName: "",
  });

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
                onTest={() => handleTest(connector)}
                onDelete={() => handleDelete(connector)}
                isTesting={testConnector.isPending}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Configuration Modal */}
      {modal.isOpen && modal.connector && (
        <div className="connector-modal-backdrop" onClick={closeModal} role="presentation">
          <div className="connector-modal" onClick={(e) => e.stopPropagation()}>
            <header className="connector-modal-header">
              <h3>Configure {modal.connector.name}</h3>
              <button type="button" className="close-btn" onClick={closeModal} aria-label="Close">
                ×
              </button>
            </header>

            <form onSubmit={handleSave}>
              <div className="connector-modal-body">
                <p className="connector-description">{modal.connector.description}</p>

                <a
                  href={modal.connector.docs_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="connector-docs-link"
                >
                  Setup documentation →
                </a>

                <div className="connector-fields">
                  <label className="form-field">
                    Display Name (optional)
                    <input
                      type="text"
                      value={modal.displayName}
                      onChange={(e) => setModal((prev) => ({ ...prev, displayName: e.target.value }))}
                      placeholder="e.g. #engineering channel"
                    />
                  </label>

                  {modal.connector.fields.map((field) => (
                    <CredentialFieldInput
                      key={field.key}
                      field={field}
                      value={modal.credentials[field.key] ?? ""}
                      onChange={(v) => handleCredentialChange(field.key, v)}
                    />
                  ))}
                </div>
              </div>

              <footer className="connector-modal-footer">
                <button type="button" className="btn-secondary" onClick={closeModal}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn-tertiary"
                  onClick={handleTestBeforeSave}
                  disabled={testBeforeSave.isPending}
                >
                  {testBeforeSave.isPending ? "Testing…" : "Test"}
                </button>
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={configureConnector.isPending}
                >
                  {configureConnector.isPending ? "Saving…" : "Save"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}
    </>
  );
}

type ConnectorCardProps = {
  connector: ConnectorStatus;
  onConfigure: () => void;
  onTest: () => void;
  onDelete: () => void;
  isTesting: boolean;
};

function ConnectorCard({ connector, onConfigure, onTest, onDelete, isTesting }: ConnectorCardProps) {
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

type CredentialFieldInputProps = {
  field: CredentialField;
  value: string;
  onChange: (value: string) => void;
};

function CredentialFieldInput({ field, value, onChange }: CredentialFieldInputProps) {
  return (
    <label className="form-field">
      {field.label}
      {field.required && <span className="required">*</span>}
      <input
        type={field.type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={field.placeholder}
        required={field.required}
      />
    </label>
  );
}

export default ConnectorCredentialsPanel;
