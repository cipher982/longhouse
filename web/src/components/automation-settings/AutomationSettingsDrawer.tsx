import { useCallback, useRef, useState, type FormEvent } from "react";
import clsx from "clsx";
import { useConfirm } from "../confirm";
import {
  useAddMcpServer,
  useAutomationDetails,
  useContainerPolicy,
  useMcpServers,
  useRemoveMcpServer,
  useTestMcpServer,
  useToolOptions,
  useDebouncedUpdateAllowedTools,
} from "../../hooks/useAutomationConfig";
import {
  useAutomationConnectors,
  useConfigureConnector,
  useTestConnectorBeforeSave,
} from "../../hooks/useAutomationConnectors";
import { useAccountConnectors } from "../../hooks/useAccountConnectors";
import { useEscapeKey } from "../../hooks/useEscapeKey";
import { useAuth } from "../../lib/auth";
import type { AccountConnectorStatus, ConnectorStatus } from "../../types/connectors";
import type { Automation, ContainerPolicy, McpServerAddRequest, McpServerResponse } from "../../services/api";
import { TOOL_GROUPS, UTILITY_TOOLS } from "../../constants/toolGroups";
import { ConnectorConfigModal, type ConfigModalState } from "./ConnectorConfigModal";
import { Link } from "react-router-dom";
import { PlugIcon } from "../icons";
import { Button } from "../ui";

type AutomationSettingsDrawerProps = {
  automationId: number;
  isOpen: boolean;
  onClose: () => void;
};

type AllowedToolOption = {
  name: string;
  label: string;
  source: "builtin" | `mcp:${string}`;
};

type AutomationSettingsDrawerContentProps = {
  automation: Automation;
  policy: ContainerPolicy | undefined;
  servers: McpServerResponse[] | undefined;
  loadingServers: boolean;
  toolOptions: AllowedToolOption[];
  debouncedUpdateAllowedTools: ReturnType<typeof useDebouncedUpdateAllowedTools>;
  addMcpServer: ReturnType<typeof useAddMcpServer>;
  removeMcpServer: ReturnType<typeof useRemoveMcpServer>;
  testMcpServer: ReturnType<typeof useTestMcpServer>;
  connectors: ConnectorStatus[] | undefined;
  accountConnectors: AccountConnectorStatus[] | undefined;
  configureConnector: ReturnType<typeof useConfigureConnector>;
  testBeforeSave: ReturnType<typeof useTestConnectorBeforeSave>;
  user: ReturnType<typeof useAuth>["user"];
  handleClose: () => Promise<void>;
};

export function AutomationSettingsDrawer({ automationId, isOpen, onClose }: AutomationSettingsDrawerProps) {
  const { user } = useAuth();
  const confirm = useConfirm();
  const { data: automation } = useAutomationDetails(isOpen ? automationId : null);
  const { data: policy } = useContainerPolicy();
  const { data: servers, isLoading: loadingServers } = useMcpServers(isOpen ? automationId : null);
  const toolOptions = useToolOptions(isOpen ? automationId : null) as AllowedToolOption[];
  const debouncedUpdateAllowedTools = useDebouncedUpdateAllowedTools(isOpen ? automationId : null);
  const addMcpServer = useAddMcpServer(isOpen ? automationId : null);
  const removeMcpServer = useRemoveMcpServer(isOpen ? automationId : null);
  const testMcpServer = useTestMcpServer(isOpen ? automationId : null);
  const { data: connectors } = useAutomationConnectors(isOpen ? automationId : null);
  const { data: accountConnectors } = useAccountConnectors();
  const configureConnector = useConfigureConnector(automationId);
  const testBeforeSave = useTestConnectorBeforeSave(automationId);

  const handleClose = useCallback(async () => {
    if (debouncedUpdateAllowedTools.hasPendingDebounce) {
      const shouldSave = await confirm({
        title: 'Unsaved changes',
        message: 'You have unsaved changes. Do you want to save them before closing?',
        confirmLabel: 'Save & Close',
        cancelLabel: 'Discard',
        variant: 'warning',
      });
      if (shouldSave) {
        debouncedUpdateAllowedTools.flush();
      } else {
        debouncedUpdateAllowedTools.cancelPending();
      }
    }

    if (debouncedUpdateAllowedTools.isPending) {
      const closeAnyway = await confirm({
        title: 'Save in progress',
        message: 'Changes are still being saved. Close anyway?',
        confirmLabel: 'Close Anyway',
        cancelLabel: 'Wait',
        variant: 'warning',
      });
      if (!closeAnyway) {
        return;
      }
    }

    onClose();
  }, [debouncedUpdateAllowedTools, onClose, confirm]);

  useEscapeKey(() => {
    void handleClose();
  }, isOpen);

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="automation-settings-backdrop open"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          void handleClose();
        }
      }}
      role="presentation"
    >
      {automation ? (
        <AutomationSettingsDrawerContent
          automation={automation}
          policy={policy}
          servers={servers}
          loadingServers={loadingServers}
          toolOptions={toolOptions}
          debouncedUpdateAllowedTools={debouncedUpdateAllowedTools}
          addMcpServer={addMcpServer}
          removeMcpServer={removeMcpServer}
          testMcpServer={testMcpServer}
          connectors={connectors}
          accountConnectors={accountConnectors}
          configureConnector={configureConnector}
          testBeforeSave={testBeforeSave}
          user={user}
          handleClose={handleClose}
        />
      ) : (
        <aside className="automation-settings-drawer open" data-testid="automation-settings-modal">
          <header className="automation-settings-header">
            <div>
              <h2>Automation Config</h2>
              <p>Loading automation…</p>
            </div>
            <button type="button" className="close-btn" onClick={() => void handleClose()} aria-label="Close settings">
              ×
            </button>
          </header>
          <section className="automation-settings-section">
            <p className="muted">Loading automation settings…</p>
          </section>
        </aside>
      )}
    </div>
  );
}

function AutomationSettingsDrawerContent({
  automation,
  policy,
  servers,
  loadingServers,
  toolOptions,
  debouncedUpdateAllowedTools,
  addMcpServer,
  removeMcpServer,
  testMcpServer,
  connectors,
  accountConnectors,
  configureConnector,
  testBeforeSave,
  user,
  handleClose,
}: AutomationSettingsDrawerContentProps) {
  const confirm = useConfirm();
  const isOwner = user?.id === automation.owner_id;
  const isConfiguredAtAccountLevel = (type: string) => {
    if (!isOwner) return false;
    return accountConnectors?.find((connector) => connector.type === type)?.configured ?? false;
  };

  const [selectedTools, setSelectedTools] = useState<Set<string>>(
    () => new Set(automation.allowed_tools ?? []),
  );
  const lastSavedToolsRef = useRef<string[]>(automation.allowed_tools ?? []);
  const [customTool, setCustomTool] = useState("");
  const [showAddForm, setShowAddForm] = useState(false);
  const [formMode, setFormMode] = useState<"preset" | "custom">("preset");
  const [presetName, setPresetName] = useState("");
  const [customName, setCustomName] = useState("");
  const [customUrl, setCustomUrl] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [formAllowedTools, setFormAllowedTools] = useState("");
  const [isTesting, setIsTesting] = useState(false);

  // Connector Config Modal State
  const [connectorModal, setConnectorModal] = useState<ConfigModalState>({
    isOpen: false,
    connector: null,
    credentials: {},
    displayName: "",
  });

  // --- Tool Logic ---

  const queueAllowedToolsUpdate = (next: Set<string>) => {
    const nextAllowedTools = Array.from(next);
    setSelectedTools(next);
    debouncedUpdateAllowedTools.mutate(nextAllowedTools, {
      onSuccess: (savedTools) => {
        lastSavedToolsRef.current = savedTools ?? nextAllowedTools;
      },
      onError: () => {
        setSelectedTools(new Set(lastSavedToolsRef.current));
      },
    });
  };

  const toggleTool = (tool: string) => {
    const next = new Set(selectedTools);
    if (next.has(tool)) {
      next.delete(tool);
    } else {
      next.add(tool);
    }
    queueAllowedToolsUpdate(next);
  };

  const handleAddCustomTool = () => {
    const trimmed = customTool.trim();
    if (!trimmed || selectedTools.has(trimmed)) {
      return;
    }
    const next = new Set(selectedTools);
    next.add(trimmed);
    queueAllowedToolsUpdate(next);
    setCustomTool("");
  };

  // --- Integration Logic ---

  const isIntegrationEnabled = (key: string) => {
    const tools = TOOL_GROUPS[key];
    if (!tools) return false;
    // Integration is "enabled" if ANY of its tools are selected.
    // Toggling it ON will add ALL. Toggling OFF will remove ALL.
    return tools.some((t) => selectedTools.has(t));
  };

  const toggleIntegration = (key: string, enabled: boolean) => {
    const tools = TOOL_GROUPS[key];
    if (!tools) return;

    const next = new Set(selectedTools);
    tools.forEach((tool) => {
      if (enabled) next.add(tool);
      else next.delete(tool);
    });
    queueAllowedToolsUpdate(next);

    if (enabled) {
      // Check if we need to configure credentials
      const connector = connectors?.find((c) => c.type === key);
      if (!connector) return;

      // If configured at account level, we don't need to prompt
      if (isConfiguredAtAccountLevel(key)) {
        return;
      }

      // If user is not the owner, we cannot know the true account-level status.
      // Only prompt for override if user is owner. Non-owners can use "Setup Override" button.
      if (isOwner && !connector.configured) {
        openConnectorModal(connector);
      }
    }
  };

  const openConnectorModal = (connector: ConnectorStatus) => {
    const initialCreds: Record<string, string> = {};
    for (const field of connector.fields) {
      initialCreds[field.key] = "";
    }
    setConnectorModal({
      isOpen: true,
      connector,
      credentials: initialCreds,
      displayName: connector.display_name ?? "",
    });
  };

  const closeConnectorModal = () => {
    setConnectorModal({
      isOpen: false,
      connector: null,
      credentials: {},
      displayName: "",
    });
  };

  // Connector Modal Handlers
  const handleConnectorSave = (e: FormEvent) => {
    e.preventDefault();
    if (!connectorModal.connector) return;
    configureConnector.mutate(
      {
        connector_type: connectorModal.connector.type,
        credentials: connectorModal.credentials,
        display_name: connectorModal.displayName || undefined,
      },
      {
        onSuccess: () => closeConnectorModal(),
      }
    );
  };

  const handleConnectorTest = () => {
    if (!connectorModal.connector) return;
    testBeforeSave.mutate({
      connector_type: connectorModal.connector.type,
      credentials: connectorModal.credentials,
    });
  };

  // --- MCP Logic ---

  const resetForm = () => {
    setShowAddForm(false);
    setPresetName("");
    setCustomName("");
    setCustomUrl("");
    setAuthToken("");
    setFormAllowedTools("");
    setFormMode("preset");
  };

  const handleSubmitServer = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const payload: McpServerAddRequest =
      formMode === "preset"
        ? {
            transport: "http",
            preset: presetName.trim(),
            auth_token: authToken.trim() || undefined,
            allowed_tools: parseAllowedTools(formAllowedTools),
          }
        : {
            transport: "http",
            name: customName.trim(),
            url: customUrl.trim(),
            auth_token: authToken.trim() || undefined,
            allowed_tools: parseAllowedTools(formAllowedTools),
          };

    addMcpServer.mutate(payload, {
      onSuccess: () => {
        resetForm();
      },
    });
  };

  const handleTestServer = () => {
    setIsTesting(true);
    const payload: McpServerAddRequest =
      formMode === "preset"
        ? {
            transport: "http",
            preset: presetName.trim(),
            auth_token: authToken.trim() || undefined,
            allowed_tools: parseAllowedTools(formAllowedTools),
          }
        : {
            transport: "http",
            name: customName.trim(),
            url: customUrl.trim(),
            auth_token: authToken.trim() || undefined,
            allowed_tools: parseAllowedTools(formAllowedTools),
          };

    testMcpServer.mutate(payload, {
      onSettled: () => setIsTesting(false),
    });
  };

  const handleRemoveServer = async (server: McpServerResponse) => {
    const confirmed = await confirm({
      title: `Remove MCP server "${server.name}"?`,
      message: 'This automation will no longer have access to tools provided by this MCP server.',
      confirmLabel: 'Remove',
      cancelLabel: 'Keep',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }
    removeMcpServer.mutate(server.name);
  };

  // --- Rendering ---

  return (
    <>
      <aside className="automation-settings-drawer open" data-testid="automation-settings-modal">
        <header className="automation-settings-header">
          <div>
            <h2>Automation Config</h2>
            <p>{automation.name}</p>
          </div>
          <button type="button" className="close-btn" onClick={() => void handleClose()} aria-label="Close settings">
            ×
          </button>
        </header>

        <section className="automation-settings-section">
          <h3>Container Execution</h3>
          <p className="section-description">
            Automations execute shell commands within ephemeral containers. Configure tool access via the allowlist below.
          </p>
          {policy ? (
            <dl className="policy-grid">
              <div>
                <dt>Status</dt>
                <dd className={policy.enabled ? "status-enabled" : "status-disabled"}>
                  {policy.enabled ? "Enabled" : "Disabled"}
                </dd>
              </div>
              <div>
                <dt>Default Image</dt>
                <dd>{policy.default_image ?? "python:3.11-slim"}</dd>
              </div>
              <div>
                <dt>Network</dt>
                <dd>{policy.network_enabled ? "Enabled" : "Disabled"}</dd>
              </div>
              <div>
                <dt>User</dt>
                <dd>{policy.user_id ?? "65532"}</dd>
              </div>
              <div>
                <dt>Memory Limit</dt>
                <dd>{policy.memory_limit ?? "512m"}</dd>
              </div>
              <div>
                <dt>CPU</dt>
                <dd>{policy.cpus ?? "0.5"}</dd>
              </div>
              <div>
                <dt>Timeout</dt>
                <dd>{policy.timeout_secs}s</dd>
              </div>
            </dl>
          ) : (
            <p className="muted">Loading container policy…</p>
          )}
        </section>

        {/* Unified Integrations Section */}
        <section className="automation-settings-section">
          <div className="section-header">
            <div>
              <h3>Integrations & Tools</h3>
              <p className="section-description">
                Enable tools and configure credentials for external services.
                <Link to="/settings/integrations" className="settings-link">
                  Manage integrations →
                </Link>
              </p>
            </div>
            {debouncedUpdateAllowedTools.isPending && (
              <span className="saving-indicator" title="Saving changes…">
                ●
              </span>
            )}
          </div>

          {/* High-Level Integrations */}
          <div className="integrations-list">
            {connectors?.map((connector) => {
              const isEnabled = isIntegrationEnabled(connector.type);
              const hasAccountCreds = isConfiguredAtAccountLevel(connector.type);
              const hasAutomationOverride = connector.configured;
              const isConfigured = hasAutomationOverride || hasAccountCreds;

              return (
                <div key={connector.type} className="integration-card">
                  <div className="integration-info">
                    <div className="integration-icon">
                      {/* Use the emoji icon from metadata if available, or fallback */}
                      {connector.icon && connector.icon.length < 5 ? connector.icon : <PlugIcon width={18} height={18} />}
                    </div>
                    <div>
                      <h4>{connector.name}</h4>
                      <p>{connector.description}</p>
                      {/* Integration status badges */}
                      {isEnabled && (
                        <div className="integration-status-badges">
                          {/* Account-level badge (only show for owner) */}
                          {isOwner && hasAccountCreds && !hasAutomationOverride && (
                            <span className="status-badge account-level" title="Using account-level credentials">
                              Account
                            </span>
                          )}
                          {/* Override badge (always valid if configured) */}
                          {hasAutomationOverride && (
                            <span className="status-badge automation-override" title="Using automation-specific credentials">
                              Override
                            </span>
                          )}
                          {/* Needs setup badge (only if we know for sure) */}
                          {isOwner && !isConfigured && (
                            <span className="status-badge needs-setup" title="Credentials not configured">
                              Needs setup
                            </span>
                          )}
                          {/* Unknown status for non-owners */}
                          {!isOwner && !hasAutomationOverride && (
                            <span className="status-badge unknown-status" title="Account-level status hidden">
                              Owner managed
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="integration-actions">
                    {isEnabled && isOwner && !hasAccountCreds && !hasAutomationOverride && (
                      <Link
                        to="/settings/integrations"
                        className={clsx("ui-button", "ui-button--secondary", "ui-button--sm")}
                      >
                        Configure
                      </Link>
                    )}
                    {isEnabled && !isOwner && !hasAutomationOverride && (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        onClick={() => openConnectorModal(connector)}
                      >
                        Setup Override
                      </Button>
                    )}
                    {isEnabled && hasAutomationOverride && (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        onClick={() => openConnectorModal(connector)}
                      >
                        Edit Override
                      </Button>
                    )}
                    <label className="switch">
                      <input
                        type="checkbox"
                        checked={isEnabled}
                        onChange={(e) => toggleIntegration(connector.type, e.target.checked)}
                      />
                      <span className="slider round" />
                    </label>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="tools-separator">
            <h4>Built-in Utilities</h4>
          </div>

          <div className="tools-list utility-tools">
             {UTILITY_TOOLS.map(toolName => (
                <label key={toolName} className="tool-option">
                  <input
                    type="checkbox"
                    checked={selectedTools.has(toolName)}
                    onChange={() => toggleTool(toolName)}
                  />
                  <span>{toolName}</span>
                </label>
             ))}
          </div>

          <details className="advanced-tools">
             <summary>Advanced / Custom Tools</summary>
             <div className="tools-list">
                {/* Render tools that aren't in ANY group or Utility list */}
                {toolOptions
                  .filter(opt => {
                     // Check if this tool is part of any known group
                     const isGrouped = Object.values(TOOL_GROUPS).some(group => group.includes(opt.name));
                     const isUtility = UTILITY_TOOLS.includes(opt.name);
                     return !isGrouped && !isUtility;
                  })
                  .map(option => {
                    const id = `tool-${option.name}`;
                    return (
                      <label key={option.name} className="tool-option" htmlFor={id}>
                        <input
                          id={id}
                          type="checkbox"
                          checked={selectedTools.has(option.name)}
                          onChange={() => toggleTool(option.name)}
                        />
                        <span>{option.label}</span>
                        <span className="tool-badge">{option.source}</span>
                      </label>
                    );
                  })
                }
             </div>
             <div className="custom-tool-input">
                <input
                  type="text"
                  placeholder="Add custom tool (e.g. http_*)"
                  value={customTool}
                  onChange={(event) => setCustomTool(event.target.value)}
                />
                <Button type="button" variant="secondary" size="md" onClick={handleAddCustomTool}>
                  Add
                </Button>
              </div>
          </details>

        </section>

        <section className="automation-settings-section">
          <header className="section-header">
            <div>
              <h3>MCP Servers</h3>
              <p className="section-description">
                Connect Model Context Protocol servers to expose additional tools to this automation.
              </p>
            </div>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                setShowAddForm(true);
              }}
            >
              Add server
            </Button>
          </header>

          {loadingServers && <p className="muted">Loading servers…</p>}
          {!loadingServers && servers && servers.length === 0 && <p className="muted">No MCP servers configured.</p>}
          {!loadingServers && servers && servers.length > 0 && (
            <ul className="mcp-server-list">
              {servers.map((server) => (
                <li key={server.name} className="mcp-server-item">
                  <div className="mcp-server-heading">
                    <div>
                      <div className="server-name">{server.name}</div>
                      <div className="server-url">{server.url}</div>
                    </div>
                    <span className={clsx("status-pill", server.status)}>
                      {server.status === "online" ? "Online" : "Offline"}
                    </span>
                  </div>
                  {server.tools.length > 0 && (
                    <div className="server-tools">
                      <span>Tools:</span>
                      <ul>
                        {server.tools.map((tool) => (
                          <li key={tool}>{tool}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div className="server-actions">
                    <Button type="button" variant="secondary" size="sm" onClick={() => handleRemoveServer(server)}>
                      Remove
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {showAddForm && (
            <form className="mcp-add-form" onSubmit={handleSubmitServer}>
              <div className="form-row">
                <label>
                  <input
                    type="radio"
                    name="server-mode"
                    checked={formMode === "preset"}
                    onChange={() => setFormMode("preset")}
                  />
                  Preset
                </label>
                <label>
                  <input
                    type="radio"
                    name="server-mode"
                    checked={formMode === "custom"}
                    onChange={() => setFormMode("custom")}
                  />
                  Custom
                </label>
              </div>

              {formMode === "preset" ? (
                <label className="form-field">
                  Preset name
                  <input
                    type="text"
                    value={presetName}
                    onChange={(event) => setPresetName(event.target.value)}
                    placeholder="e.g. github"
                    required
                  />
                </label>
              ) : (
                <>
                  <label className="form-field">
                    Server name
                    <input
                      type="text"
                      value={customName}
                      onChange={(event) => setCustomName(event.target.value)}
                      placeholder="my-server"
                      required
                    />
                  </label>
                  <label className="form-field">
                    Server URL
                    <input
                      type="url"
                      value={customUrl}
                      onChange={(event) => setCustomUrl(event.target.value)}
                      placeholder="https://example.com/mcp"
                      required
                    />
                  </label>
                </>
              )}

              <label className="form-field">
                Auth token
                <input
                  type="password"
                  value={authToken}
                  onChange={(event) => setAuthToken(event.target.value)}
                  placeholder="Optional"
                />
              </label>

              <label className="form-field">
                Allowed tools (comma separated)
                <input
                  type="text"
                  value={formAllowedTools}
                  onChange={(event) => setFormAllowedTools(event.target.value)}
                  placeholder="e.g. create_issue, search_repositories"
                />
              </label>

              <div className="form-actions">
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    resetForm();
                  }}
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  variant="tertiary"
                  onClick={handleTestServer}
                  disabled={isTesting}
                >
                  {isTesting ? "Testing…" : "Test connection"}
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={addMcpServer.isPending}
                >
                  {addMcpServer.isPending ? "Adding…" : "Add server"}
                </Button>
              </div>
            </form>
          )}
        </section>

        <footer className="automation-settings-footer">
          <Button type="button" variant="primary" onClick={() => void handleClose()}>
            Close
          </Button>
        </footer>
      </aside>

      {/* Config Modal */}
      <ConnectorConfigModal
        modal={connectorModal}
        onClose={closeConnectorModal}
        onSave={handleConnectorSave}
        onTest={handleConnectorTest}
        onCredentialChange={(key, value) =>
          setConnectorModal((prev) => ({
            ...prev,
            credentials: { ...prev.credentials, [key]: value },
          }))
        }
        onDisplayNameChange={(value) =>
           setConnectorModal((prev) => ({ ...prev, displayName: value }))
        }
        isSaving={configureConnector.isPending}
        isTesting={testBeforeSave.isPending}
      />
    </>
  );
}

function parseAllowedTools(input: string): string[] | undefined {
  const values = input
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return values.length > 0 ? values : undefined;
}

export default AutomationSettingsDrawer;
