import React, { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { getUserContext, updateUserContext, type UserContext } from "../services/api";

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

  // Form state
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("");
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");
  const [customInstructions, setCustomInstructions] = useState("");
  const [servers, setServers] = useState<Server[]>([]);
  const [integrations, setIntegrations] = useState<Record<string, string>>({});

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
      <div className="profile-container">
        <div>Loading settings...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="profile-container">
        <div className="profile-content">
          <h2>Settings</h2>
          <p className="error-message">Failed to load settings: {String(error)}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="profile-container">
      <div className="profile-content">
        <h2>User Context Settings</h2>
        <p className="settings-description">
          Configure your personal context that AI agents will use to better understand and assist you.
        </p>

        <form onSubmit={handleSubmit} className="profile-form">
          {/* Basic Information */}
          <div className="form-section">
            <h3>Basic Information</h3>

            <div className="form-group">
              <label htmlFor="display-name" className="form-label">Display Name</label>
              <input
                type="text"
                id="display-name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Your name"
                className="form-input"
              />
              <small>How you'd like to be addressed</small>
            </div>

            <div className="form-group">
              <label htmlFor="role" className="form-label">Role</label>
              <input
                type="text"
                id="role"
                value={role}
                onChange={(e) => setRole(e.target.value)}
                placeholder="e.g., software engineer, founder, student"
                className="form-input"
              />
              <small>Your professional role or position</small>
            </div>

            <div className="form-group">
              <label htmlFor="location" className="form-label">Location</label>
              <input
                type="text"
                id="location"
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                placeholder="e.g., San Francisco, CA"
                className="form-input"
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
                className="form-input"
                rows={3}
              />
              <small>What you do and how AI agents can help you</small>
            </div>
          </div>

          {/* Servers */}
          <div className="form-section">
            <div className="section-header">
              <h3>Servers</h3>
              <button
                type="button"
                onClick={addServer}
                className="btn-add"
              >
                + Add Server
              </button>
            </div>
            <p className="section-description">
              Configure servers that AI agents can reference or SSH into
            </p>

            {servers.length === 0 ? (
              <p className="empty-state">No servers configured</p>
            ) : (
              <div className="servers-list">
                {servers.map((server, index) => (
                  <div key={index} className="server-card">
                    <div className="server-card-header">
                      <span className="server-index">Server {index + 1}</span>
                      <button
                        type="button"
                        onClick={() => removeServer(index)}
                        className="btn-remove"
                      >
                        Remove
                      </button>
                    </div>

                    <div className="server-fields">
                      <div className="form-group">
                        <label className="form-label">Name *</label>
                        <input
                          type="text"
                          value={server.name}
                          onChange={(e) => updateServer(index, "name", e.target.value)}
                          placeholder="e.g., production-server"
                          className="form-input"
                          required
                        />
                      </div>

                      <div className="form-group">
                        <label className="form-label">IP Address *</label>
                        <input
                          type="text"
                          value={server.ip}
                          onChange={(e) => updateServer(index, "ip", e.target.value)}
                          placeholder="e.g., 192.168.1.100"
                          className="form-input"
                          required
                        />
                      </div>

                      <div className="form-group">
                        <label className="form-label">Purpose *</label>
                        <input
                          type="text"
                          value={server.purpose}
                          onChange={(e) => updateServer(index, "purpose", e.target.value)}
                          placeholder="e.g., production web server"
                          className="form-input"
                          required
                        />
                      </div>

                      <div className="form-group">
                        <label className="form-label">Platform</label>
                        <input
                          type="text"
                          value={server.platform || ""}
                          onChange={(e) => updateServer(index, "platform", e.target.value)}
                          placeholder="e.g., Ubuntu 22.04"
                          className="form-input"
                        />
                      </div>

                      <div className="form-group">
                        <label className="form-label">Notes</label>
                        <textarea
                          value={server.notes || ""}
                          onChange={(e) => updateServer(index, "notes", e.target.value)}
                          placeholder="Additional notes about this server"
                          className="form-input"
                          rows={2}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Integrations */}
          <div className="form-section">
            <h3>Integrations</h3>
            <p className="section-description">
              Tools and services you use (e.g., health_tracker: WHOOP, notes: Obsidian)
            </p>

            {Object.keys(integrations).length === 0 ? (
              <p className="empty-state">No integrations configured</p>
            ) : (
              <div className="integrations-list">
                {Object.entries(integrations).map(([key, value]) => (
                  <div key={key} className="integration-item">
                    <div className="integration-info">
                      <span className="integration-key">{key}:</span>
                      <span className="integration-value">{value}</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => removeIntegration(key)}
                      className="btn-remove-small"
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="add-integration">
              <div className="form-group">
                <label className="form-label">Key</label>
                <input
                  type="text"
                  value={newIntegrationKey}
                  onChange={(e) => setNewIntegrationKey(e.target.value)}
                  placeholder="e.g., health_tracker"
                  className="form-input"
                />
              </div>
              <div className="form-group">
                <label className="form-label">Value</label>
                <input
                  type="text"
                  value={newIntegrationValue}
                  onChange={(e) => setNewIntegrationValue(e.target.value)}
                  placeholder="e.g., WHOOP"
                  className="form-input"
                />
              </div>
              <button
                type="button"
                onClick={addIntegration}
                className="btn-add"
              >
                Add Integration
              </button>
            </div>
          </div>

          {/* Custom Instructions */}
          <div className="form-section">
            <h3>Custom Instructions</h3>
            <p className="section-description">
              Specific preferences for how AI agents should respond to you
            </p>

            <div className="form-group">
              <textarea
                id="custom-instructions"
                value={customInstructions}
                onChange={(e) => setCustomInstructions(e.target.value)}
                placeholder="e.g., Always explain technical concepts in detail, prefer Python over JavaScript, etc."
                className="form-input"
                rows={4}
              />
            </div>
          </div>

          {/* Form Actions */}
          <div className="form-actions">
            <button
              type="button"
              onClick={handleReset}
              className="btn-secondary"
              disabled={updateMutation.isPending}
            >
              Reset Changes
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? "Saving..." : "Save Settings"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
