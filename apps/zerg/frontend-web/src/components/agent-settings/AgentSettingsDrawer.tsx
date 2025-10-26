import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import clsx from "clsx";
import {
  useAddMcpServer,
  useAgentDetails,
  useAvailableTools,
  useContainerPolicy,
  useMcpServers,
  useRemoveMcpServer,
  useTestMcpServer,
  useToolOptions,
  useDebouncedUpdateAllowedTools,
} from "../../hooks/useAgentTooling";
import type { McpServerAddRequest, McpServerResponse } from "../../services/api";

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
  const { data: agent } = useAgentDetails(isOpen ? agentId : null);
  const { data: policy } = useContainerPolicy();
  const { data: servers, isLoading: loadingServers } = useMcpServers(isOpen ? agentId : null);
  const { data: availableTools } = useAvailableTools(isOpen ? agentId : null);
  const toolOptions = useToolOptions(isOpen ? agentId : null) as AllowedToolOption[];
  const debouncedUpdateAllowedTools = useDebouncedUpdateAllowedTools(isOpen ? agentId : null);
  const addMcpServer = useAddMcpServer(isOpen ? agentId : null);
  const removeMcpServer = useRemoveMcpServer(isOpen ? agentId : null);
  const testMcpServer = useTestMcpServer(isOpen ? agentId : null);

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

  // Unified close handler that guards all close paths
  const handleClose = useCallback(() => {
    // Cancel pending debounce timer
    debouncedUpdateAllowedTools.cancelPending();

    // Check if mutation is in-flight
    if (debouncedUpdateAllowedTools.isPending) {
      const confirmed = window.confirm("Save in progress. Close anyway?");
      if (!confirmed) {
        return;
      }
    }

    onClose();
  }, [debouncedUpdateAllowedTools, onClose]);

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

  const builtinTools = useMemo(() => (availableTools ? availableTools.builtin : []), [availableTools]);
  const mcpTools = useMemo(() => availableTools?.mcp ?? {}, [availableTools]);

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
      // Auto-save via debounced mutation
      debouncedUpdateAllowedTools.mutate(Array.from(next));
      return next;
    });
    setCustomTool("");
  };

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

  const handleRemoveServer = (server: McpServerResponse) => {
    const confirmed =
      typeof window === "undefined" ||
      window.confirm(`Remove MCP server "${server.name}" from this agent?`);
    if (!confirmed) {
      return;
    }
    removeMcpServer.mutate(server.name);
  };

  const renderToolOption = (option: AllowedToolOption) => {
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
  };

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
      <aside className={clsx("agent-settings-drawer", { open: isOpen })}>
        <header className="agent-settings-header">
          <div>
            <h2>Agent Tooling</h2>
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

        <section className="agent-settings-section">
          <div className="section-header">
            <div>
              <h3>Allowed Tools</h3>
              <p className="section-description">
                Select which tools this agent can invoke. Leave empty to allow all tools. You can also add wildcard entries.
              </p>
            </div>
            {debouncedUpdateAllowedTools.isPending && (
              <span className="saving-indicator" title="Saving changes…">
                ●
              </span>
            )}
          </div>
          <div className="tools-list">
            {toolOptions.map(renderToolOption)}
          </div>
          <div className="custom-tool-input">
            <input
              type="text"
              placeholder="Add custom tool (e.g. http_*)"
              value={customTool}
              onChange={(event) => setCustomTool(event.target.value)}
            />
            <button type="button" onClick={handleAddCustomTool}>
              Add
            </button>
          </div>
        </section>

        <section className="agent-settings-section">
          <header className="section-header">
            <div>
              <h3>MCP Servers</h3>
              <p className="section-description">
                Connect Model Context Protocol servers to expose additional tools to this agent.
              </p>
            </div>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                setShowAddForm(true);
              }}
            >
              Add server
            </button>
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
                    <button type="button" className="btn-secondary" onClick={() => handleRemoveServer(server)}>
                      Remove
                    </button>
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
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => {
                    resetForm();
                  }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn-tertiary"
                  onClick={handleTestServer}
                  disabled={isTesting}
                >
                  {isTesting ? "Testing…" : "Test connection"}
                </button>
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={addMcpServer.isPending}
                >
                  {addMcpServer.isPending ? "Adding…" : "Add server"}
                </button>
              </div>
            </form>
          )}
        </section>

        <footer className="agent-settings-footer">
          <button type="button" className="btn-primary" onClick={handleClose}>
            Close
          </button>
        </footer>
      </aside>
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
