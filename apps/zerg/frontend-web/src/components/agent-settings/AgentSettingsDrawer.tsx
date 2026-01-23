import { useCallback, useEffect, useState, type FormEvent } from "react";
import clsx from "clsx";
import { useConfirm } from "../confirm";
import {
  useAddMcpServer,
  useAgentDetails,
  useContainerPolicy,
  useMcpServers,
  useRemoveMcpServer,
  useTestMcpServer,
  useToolOptions,
  useDebouncedUpdateAllowedTools,
} from "../../hooks/useAgentConfig";
import {
  useAgentConnectors,
  useConfigureConnector,
  useTestConnectorBeforeSave,
} from "../../hooks/useAgentConnectors";
import { useAccountConnectors } from "../../hooks/useAccountConnectors";
import { useAuth } from "../../lib/auth";
import type { McpServerAddRequest, McpServerResponse } from "../../services/api";
import { TOOL_GROUPS, UTILITY_TOOLS } from "../../constants/toolGroups";
import { ConnectorConfigModal, type ConfigModalState } from "./ConnectorConfigModal";
import type { ConnectorStatus } from "../../types/connectors";
import { Link } from "react-router-dom";
import { PlugIcon } from "../icons";
import { Button } from "../ui";

type AgentSettingsDrawerProps = {
  agentId: number;
  isOpen: boolean;
  onClose: () => void;
};

type AllowedToolOption = {
  name: string;
  label: string;
  source: "builtin" | `mcp:${string}`;
};

export function AgentSettingsDrawer({ agentId, isOpen, onClose }: AgentSettingsDrawerProps) {
  const { user } = useAuth();
  const confirm = useConfirm();
  const { data: agent } = useAgentDetails(isOpen ? agentId : null);
  const { data: policy } = useContainerPolicy();
  const { data: servers, isLoading: loadingServers } = useMcpServers(isOpen ? agentId : null);
  const toolOptions = useToolOptions(isOpen ? agentId : null) as AllowedToolOption[];
  const debouncedUpdateAllowedTools = useDebouncedUpdateAllowedTools(isOpen ? agentId : null);
  const addMcpServer = useAddMcpServer(isOpen ? agentId : null);
  const removeMcpServer = useRemoveMcpServer(isOpen ? agentId : null);
  const testMcpServer = useTestMcpServer(isOpen ? agentId : null);

  // Connector Hooks
  const { data: connectors } = useAgentConnectors(isOpen ? agentId : null);
  const { data: accountConnectors } = useAccountConnectors();
  const configureConnector = useConfigureConnector(agentId);
  const testBeforeSave = useTestConnectorBeforeSave(agentId);

  // Helper to check ownership
  const isOwner = user?.id === agent?.owner_id;

  // Helper to check if a connector is configured at account level
  // Only valid if current user is the owner (since accountConnectors fetches MY connectors)
  const isConfiguredAtAccountLevel = (type: string) => {
    if (!isOwner) return false;
    return accountConnectors?.find((c) => c.type === type)?.configured ?? false;
  };

  const [selectedTools, setSelectedTools] = useState<Set<string>>(new Set());
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

  // Unified close handler that guards all close paths
  const handleClose = useCallback(async () => {
    // Check if debounce timer is active (user has unsaved changes)
    if (debouncedUpdateAllowedTools.hasPendingDebounce) {
      const shouldSave = await confirm({
        title: 'Unsaved changes',
        message: 'You have unsaved changes. Do you want to save them before closing?',
        confirmLabel: 'Save & Close',
        cancelLabel: 'Discard',
        variant: 'warning',
      });
      if (shouldSave) {
        // Flush pending changes immediately
        debouncedUpdateAllowedTools.flush();
        // Note: drawer will close, mutation happens in background
        // User will see save indicator if they reopen quickly
      } else {
        // User chose to discard changes
        debouncedUpdateAllowedTools.cancelPending();
      }
    }

    // Check if mutation is in-flight
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

  // Rehydrate selectedTools from server state when drawer opens
  useEffect(() => {
    if (!isOpen) {
      return;
    }
    const tools = agent?.allowed_tools ?? [];
    setSelectedTools(new Set(tools));
  }, [agent?.allowed_tools, isOpen]);

  // Rollback optimistic updates on error
  useEffect(() => {
    if (debouncedUpdateAllowedTools.isError && agent?.allowed_tools) {
      // Restore last known good state from server
      setSelectedTools(new Set(agent.allowed_tools));
    }
  }, [debouncedUpdateAllowedTools.isError, agent?.allowed_tools]);

  // ESC key handler
  useEffect(() => {
    if (!isOpen) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        handleClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen, handleClose]);

  // --- Tool Logic ---

  const toggleTool = (tool: string) => {
    setSelectedTools((prev) => {
      const next = new Set(prev);
      if (next.has(tool)) {
        next.delete(tool);
      } else {
        next.add(tool);
      }
      // Auto-save via debounced mutation
      debouncedUpdateAllowedTools.mutate(Array.from(next));
      return next;
    });
  };

  const handleAddCustomTool = () => {
    const trimmed = customTool.trim();
    if (!trimmed) {
      return;
    }
    setSelectedTools((prev) => {
      const next = new Set(prev).add(trimmed);
      debouncedUpdateAllowedTools.mutate(Array.from(next));
      return next;
    });
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

    setSelectedTools((prev) => {
      const next = new Set(prev);
      tools.forEach((t) => {
        if (enabled) next.add(t);
        else next.delete(t);
      });
      debouncedUpdateAllowedTools.mutate(Array.from(next));
      return next;
    });

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
            preset: presetName.trim(),
            auth_token: authToken.trim() || undefined,
            allowed_tools: parseAllowedTools(formAllowedTools),
          }
        : {
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
            preset: presetName.trim(),
            auth_token: authToken.trim() || undefined,
            allowed_tools: parseAllowedTools(formAllowedTools),
          }
        : {
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
      message: 'This agent will no longer have access to tools provided by this MCP server.',
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
    <div
      className={clsx("agent-settings-backdrop", { open: isOpen })}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          handleClose();
        }
      }}
      role="presentation"
    >
      <aside className={clsx("agent-settings-drawer", { open: isOpen })} data-testid="agent-debug-modal">
        <header className="agent-settings-header">
          <div>
            <h2>Agent Config</h2>
            <p>{agent?.name}</p>
          </div>
          <button type="button" className="close-btn" onClick={handleClose} aria-label="Close settings">
            ×
          </button>
        </header>

        <section className="agent-settings-section">
          <h3>Container Execution</h3>
          <p className="section-description">
            Agents execute shell commands within ephemeral containers. Configure tool access via the allowlist below.
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
        <section className="agent-settings-section">
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
              const hasAgentOverride = connector.configured;
              const isConfigured = hasAgentOverride || hasAccountCreds;

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
                          {isOwner && hasAccountCreds && !hasAgentOverride && (
                            <span className="status-badge account-level" title="Using account-level credentials">
                              Account
                            </span>
                          )}
                          {/* Override badge (always valid if configured) */}
                          {hasAgentOverride && (
                            <span className="status-badge agent-override" title="Using agent-specific credentials">
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
                          {!isOwner && !hasAgentOverride && (
                            <span className="status-badge unknown-status" title="Account-level status hidden">
                              Owner managed
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="integration-actions">
                    {isEnabled && isOwner && !hasAccountCreds && !hasAgentOverride && (
                      <Link
                        to="/settings/integrations"
                        className={clsx("ui-button", "ui-button--secondary", "ui-button--sm")}
                      >
                        Configure
                      </Link>
                    )}
                    {isEnabled && !isOwner && !hasAgentOverride && (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        onClick={() => openConnectorModal(connector)}
                      >
                        Setup Override
                      </Button>
                    )}
                    {isEnabled && hasAgentOverride && (
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

        <section className="agent-settings-section">
          <header className="section-header">
            <div>
              <h3>MCP Servers</h3>
              <p className="section-description">
                Connect Model Context Protocol servers to expose additional tools to this agent.
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

        <footer className="agent-settings-footer">
          <Button type="button" variant="primary" onClick={handleClose}>
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
    </div>
  );
}

function parseAllowedTools(input: string): string[] | undefined {
  const values = input
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return values.length > 0 ? values : undefined;
}

export default AgentSettingsDrawer;
