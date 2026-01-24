import React, { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { getUserContext, updateUserContext, type UserContext } from "../services/api";
import {
  Button,
  Card,
  SectionHeader,
  EmptyState,
  Input,
  PageShell,
  Spinner
} from "../components/ui";
import { PlusIcon } from "../components/icons";

interface Server {
  name: string;
  ip: string;
  purpose: string;
  platform?: string;
  notes?: string;
}

export default function SettingsPage() {
  const queryClient = useQueryClient();

  // Fetch user context
  const { data, isLoading, error } = useQuery({
    queryKey: ["user-context"],
    queryFn: getUserContext,
  });

  useEffect(() => {
    if (isLoading) {
      document.body.removeAttribute("data-ready");
      return;
    }

    document.body.setAttribute("data-ready", "true");

    return () => {
      document.body.removeAttribute("data-ready");
    };
  }, [isLoading]);

  // Form state
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("");
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");
  const [customInstructions, setCustomInstructions] = useState("");
  const [servers, setServers] = useState<Server[]>([]);
  const [integrations, setIntegrations] = useState<Record<string, string>>({});
  // Tools state preserves unknown keys for forward compatibility
  // Known keys have UI toggles; unknown keys are preserved on save
  const [tools, setTools] = useState<Record<string, boolean>>({
    location: true,
    whoop: true,
    obsidian: true,
    supervisor: true,
  });

  // Track if we need to show the "add integration" form
  const [newIntegrationKey, setNewIntegrationKey] = useState("");
  const [newIntegrationValue, setNewIntegrationValue] = useState("");

  // Populate form when data loads
  useEffect(() => {
    if (data?.context) {
      const ctx = data.context;
      setDisplayName(ctx.display_name || "");
      setRole(ctx.role || "");
      setLocation(ctx.location || "");
      setDescription(ctx.description || "");
      setCustomInstructions(ctx.custom_instructions || "");
      setServers(ctx.servers || []);
      setIntegrations(ctx.integrations || {});
      // Preserve ALL tool keys from context, applying defaults only for known tools
      // This ensures custom/future tools are not lost on save
      setTools({
        // Start with any existing tools from the context
        ...(ctx.tools || {}),
        // Apply defaults for known tools (only if not already set)
        location: ctx.tools?.location ?? true,
        whoop: ctx.tools?.whoop ?? true,
        obsidian: ctx.tools?.obsidian ?? true,
        supervisor: ctx.tools?.supervisor ?? true,
      });
    }
  }, [data]);

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: updateUserContext,
    onSuccess: () => {
      toast.success("Settings saved successfully!");
      queryClient.invalidateQueries({ queryKey: ["user-context"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to save settings: ${error.message}`);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    const context: UserContext = {
      display_name: displayName || undefined,
      role: role || undefined,
      location: location || undefined,
      description: description || undefined,
      servers: servers.length > 0 ? servers : undefined,
      integrations: Object.keys(integrations).length > 0 ? integrations : undefined,
      custom_instructions: customInstructions || undefined,
      tools: tools,
    };

    updateMutation.mutate(context);
  };

  const handleReset = () => {
    if (data?.context) {
      const ctx = data.context;
      setDisplayName(ctx.display_name || "");
      setRole(ctx.role || "");
      setLocation(ctx.location || "");
      setDescription(ctx.description || "");
      setCustomInstructions(ctx.custom_instructions || "");
      setServers(ctx.servers || []);
      setIntegrations(ctx.integrations || {});
      // Preserve ALL tool keys (same logic as initial load)
      setTools({
        ...(ctx.tools || {}),
        location: ctx.tools?.location ?? true,
        whoop: ctx.tools?.whoop ?? true,
        obsidian: ctx.tools?.obsidian ?? true,
        supervisor: ctx.tools?.supervisor ?? true,
      });
    }
  };

  // Server management
  const addServer = () => {
    setServers([...servers, { name: "", ip: "", purpose: "", platform: "", notes: "" }]);
  };

  const removeServer = (index: number) => {
    setServers(servers.filter((_, i) => i !== index));
  };

  const updateServer = (index: number, field: keyof Server, value: string) => {
    const updated = [...servers];
    updated[index] = { ...updated[index], [field]: value };
    setServers(updated);
  };

  // Integration management
  const addIntegration = () => {
    if (!newIntegrationKey.trim()) {
      toast.error("Integration key is required");
      return;
    }
    setIntegrations({
      ...integrations,
      [newIntegrationKey.trim()]: newIntegrationValue.trim(),
    });
    setNewIntegrationKey("");
    setNewIntegrationValue("");
  };

  const removeIntegration = (key: string) => {
    const updated = { ...integrations };
    delete updated[key];
    setIntegrations(updated);
  };

  if (isLoading) {
    return (
      <div className="settings-page-container">
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading settings..."
          description="Fetching your personal context."
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="settings-page-container">
        <EmptyState
          variant="error"
          title="Error loading settings"
          description={String(error)}
        />
      </div>
    );
  }

  return (
    <PageShell size="narrow" className="settings-page-container">
      <SectionHeader
        title="User Context Settings"
        description="Configure your personal context that AI agents will use to better understand and assist you."
      />

      <form onSubmit={handleSubmit} className="profile-form">
        <div className="settings-stack settings-stack--lg">
          {/* Basic Information */}
          <Card>
            <Card.Header>
              <h3 className="settings-section-title ui-section-title">Basic Information</h3>
            </Card.Header>
            <Card.Body>
              <div className="settings-stack settings-stack--md">
                <div className="form-group">
                  <label htmlFor="display-name" className="form-label">Display Name</label>
                  <Input
                    id="display-name"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="Your name"
                  />
                  <small>How you'd like to be addressed</small>
                </div>

                <div className="form-group">
                  <label htmlFor="role" className="form-label">Role</label>
                  <Input
                    id="role"
                    value={role}
                    onChange={(e) => setRole(e.target.value)}
                    placeholder="e.g., software engineer, founder, student"
                  />
                  <small>Your professional role or position</small>
                </div>

                <div className="form-group">
                  <label htmlFor="location" className="form-label">Location</label>
                  <Input
                    id="location"
                    value={location}
                    onChange={(e) => setLocation(e.target.value)}
                    placeholder="e.g., San Francisco, CA"
                  />
                  <small>Your location for timezone and regional context</small>
                </div>

                <div className="form-group">
                  <label htmlFor="description" className="form-label">Description</label>
                  <textarea
                    id="description"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Brief description of what you do and what you want AI to help with"
                    className="ui-input settings-textarea"
                    rows={3}
                  />
                  <small>What you do and how AI agents can help you</small>
                </div>
              </div>
            </Card.Body>
          </Card>

          {/* Servers */}
          <Card>
            <Card.Header>
              <h3 className="settings-section-title ui-section-title">Servers</h3>
              <Button type="button" onClick={addServer}>
                <PlusIcon /> Add Server
              </Button>
            </Card.Header>
            <Card.Body>
              <p className="section-description">
                Configure servers that AI agents can reference or SSH into
              </p>

              {servers.length === 0 ? (
                <div className="settings-empty-state">
                  <p>No servers configured</p>
                </div>
              ) : (
                <div className="servers-list">
                  {servers.map((server, index) => (
                    <Card key={index} variant="default" className="settings-card-muted">
                      <Card.Header>
                        <span className="server-index">Server {index + 1}</span>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => removeServer(index)}
                        >
                          Remove
                        </Button>
                      </Card.Header>
                      <Card.Body>
                        <div className="server-fields">
                          <div className="form-group">
                            <label className="form-label">Name *</label>
                            <Input
                              value={server.name}
                              onChange={(e) => updateServer(index, "name", e.target.value)}
                              placeholder="e.g., production-server"
                              required
                            />
                          </div>

                          <div className="form-group">
                            <label className="form-label">IP Address *</label>
                            <Input
                              value={server.ip}
                              onChange={(e) => updateServer(index, "ip", e.target.value)}
                              placeholder="e.g., 192.168.1.100"
                              required
                            />
                          </div>

                          <div className="form-group">
                            <label className="form-label">Purpose *</label>
                            <Input
                              value={server.purpose}
                              onChange={(e) => updateServer(index, "purpose", e.target.value)}
                              placeholder="e.g., production web server"
                              required
                            />
                          </div>

                          <div className="form-group">
                            <label className="form-label">Platform</label>
                            <Input
                              value={server.platform || ""}
                              onChange={(e) => updateServer(index, "platform", e.target.value)}
                              placeholder="e.g., Ubuntu 22.04"
                            />
                          </div>

                          <div className="form-group form-group--full">
                            <label className="form-label">Notes</label>
                            <textarea
                              value={server.notes || ""}
                              onChange={(e) => updateServer(index, "notes", e.target.value)}
                              placeholder="Additional notes about this server"
                              className="ui-input settings-textarea settings-textarea--compact"
                              rows={2}
                            />
                          </div>
                        </div>
                      </Card.Body>
                    </Card>
                  ))}
                </div>
              )}
            </Card.Body>
          </Card>

          {/* Integrations */}
          <Card>
            <Card.Header>
              <h3 className="settings-section-title ui-section-title">Integrations</h3>
            </Card.Header>
            <Card.Body>
              <p className="section-description">
                Tools and services you use (e.g., health_tracker: WHOOP, notes: Obsidian)
              </p>

              {Object.keys(integrations).length === 0 ? (
                <div className="settings-empty-state settings-empty-state--spaced">
                  <p>No integrations configured</p>
                </div>
              ) : (
                <div className="integrations-list">
                  {Object.entries(integrations).map(([key, value]) => (
                    <div key={key} className="integration-item">
                      <div className="integration-info">
                        <span className="integration-key">{key}:</span>
                        <span className="integration-value">{value}</span>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => removeIntegration(key)}
                      >
                        Remove
                      </Button>
                    </div>
                  ))}
                </div>
              )}

              <div className="add-integration">
                <div className="form-group">
                  <label className="form-label">Key</label>
                  <Input
                    value={newIntegrationKey}
                    onChange={(e) => setNewIntegrationKey(e.target.value)}
                    placeholder="e.g., health_tracker"
                  />
                </div>
                <div className="form-group">
                  <label className="form-label">Value</label>
                  <Input
                    value={newIntegrationValue}
                    onChange={(e) => setNewIntegrationValue(e.target.value)}
                    placeholder="e.g., WHOOP"
                  />
                </div>
                <Button type="button" onClick={addIntegration}>
                  Add Integration
                </Button>
              </div>
            </Card.Body>
          </Card>

          {/* Jarvis Tools */}
          <Card>
            <Card.Header>
              <h3 className="settings-section-title ui-section-title">Jarvis Tools</h3>
            </Card.Header>
            <Card.Body>
              <p className="section-description">
                Enable or disable tools that Jarvis can use to help you
              </p>

              <div className="tools-list">
                {[
                  { id: 'location', name: 'Location', desc: 'Get current GPS location via Traccar' },
                  { id: 'whoop', name: 'WHOOP Health Data', desc: 'Get WHOOP health metrics and recovery data' },
                  { id: 'obsidian', name: 'Obsidian Notes', desc: 'Search and read notes from your Obsidian vault' },
                  { id: 'supervisor', name: 'Supervisor Agent', desc: 'Delegate complex multi-step tasks to supervisor' }
                ].map(tool => (
                  <div key={tool.id} className="tool-toggle">
                    <label className="tool-label">
                      <input
                        type="checkbox"
                        checked={tools[tool.id]}
                        onChange={(e) => setTools({ ...tools, [tool.id]: e.target.checked })}
                        className="tool-checkbox"
                      />
                      <div className="tool-info">
                        <span className="tool-name">{tool.name}</span>
                        <span className="tool-description">{tool.desc}</span>
                      </div>
                    </label>
                  </div>
                ))}
              </div>
            </Card.Body>
          </Card>

          {/* Custom Instructions */}
          <Card>
            <Card.Header>
              <h3 className="settings-section-title ui-section-title">Custom Instructions</h3>
            </Card.Header>
            <Card.Body>
              <p className="section-description">
                Specific preferences for how AI agents should respond to you
              </p>

              <div className="form-group">
                <textarea
                  id="custom-instructions"
                  value={customInstructions}
                  onChange={(e) => setCustomInstructions(e.target.value)}
                  placeholder="e.g., Always explain technical concepts in detail, prefer Python over JavaScript, etc."
                  className="ui-input settings-textarea settings-textarea--lg"
                  rows={4}
                />
              </div>
            </Card.Body>
          </Card>

          {/* Form Actions */}
          <div className="form-actions settings-form-actions">
            <Button
              type="button"
              variant="ghost"
              onClick={handleReset}
              disabled={updateMutation.isPending}
            >
              Reset Changes
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="lg"
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? "Saving..." : "Save Settings"}
            </Button>
          </div>
        </div>
      </form>
    </PageShell>
  );
}
