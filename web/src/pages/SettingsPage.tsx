import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { getUserContext, updateUserContext, type UserContext } from "../services/api";
import { useReadinessFlag } from "../lib/readiness-contract";
import {
  Button,
  Card,
  SectionHeader,
  EmptyState,
  Input,
  PageShell,
  Spinner,
} from "../components/ui";
import { PlusIcon } from "../components/icons";
import EmailConfigCard from "../components/EmailConfigCard";
import LlmProviderCard from "../components/LlmProviderCard";

interface Server {
  name: string;
  ip: string;
  purpose: string;
  platform?: string;
  notes?: string;
}

interface SettingsFormState {
  displayName: string;
  role: string;
  location: string;
  description: string;
  customInstructions: string;
  servers: Server[];
  integrations: Record<string, string>;
  tools: Record<string, boolean>;
}

const TOOL_DEFINITIONS = [
  { id: "whoop", name: "WHOOP Health Data", desc: "Get WHOOP health metrics and recovery data" },
  { id: "obsidian", name: "Obsidian Notes", desc: "Search and read notes from your Obsidian vault" },
  { id: "oikos", name: "Oikos", desc: "Delegate complex multi-step tasks to Oikos" },
];

function buildToolsState(tools?: UserContext["tools"]): Record<string, boolean> {
  return {
    ...(tools || {}),
    whoop: tools?.whoop ?? true,
    obsidian: tools?.obsidian ?? true,
    oikos: tools?.oikos ?? true,
  };
}

function createSettingsFormState(context?: UserContext | null): SettingsFormState {
  return {
    displayName: context?.display_name || "",
    role: context?.role || "",
    location: context?.location || "",
    description: context?.description || "",
    customInstructions: context?.custom_instructions || "",
    servers: context?.servers?.map((server) => ({ ...server })) || [],
    integrations: { ...(context?.integrations || {}) },
    tools: buildToolsState(context?.tools),
  };
}

function buildUserContext(formState: SettingsFormState): UserContext {
  return {
    display_name: formState.displayName || undefined,
    role: formState.role || undefined,
    location: formState.location || undefined,
    description: formState.description || undefined,
    servers: formState.servers.length > 0 ? formState.servers : undefined,
    integrations:
      Object.keys(formState.integrations).length > 0 ? formState.integrations : undefined,
    custom_instructions: formState.customInstructions || undefined,
    tools: formState.tools,
  };
}

function SettingsForm({
  initialState,
  isSaving,
  onSubmit,
}: {
  initialState: SettingsFormState;
  isSaving: boolean;
  onSubmit: (context: UserContext) => void;
}) {
  const [formState, setFormState] = useState<SettingsFormState>(initialState);
  const [newIntegrationKey, setNewIntegrationKey] = useState("");
  const [newIntegrationValue, setNewIntegrationValue] = useState("");

  const updateFormState = <K extends keyof SettingsFormState>(
    key: K,
    value: SettingsFormState[K],
  ) => {
    setFormState((current) => ({ ...current, [key]: value }));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit(buildUserContext(formState));
  };

  const handleReset = () => {
    setFormState(initialState);
    setNewIntegrationKey("");
    setNewIntegrationValue("");
  };

  const addServer = () => {
    updateFormState("servers", [
      ...formState.servers,
      { name: "", ip: "", purpose: "", platform: "", notes: "" },
    ]);
  };

  const removeServer = (index: number) => {
    updateFormState(
      "servers",
      formState.servers.filter((_, i) => i !== index),
    );
  };

  const updateServer = (index: number, field: keyof Server, value: string) => {
    updateFormState(
      "servers",
      formState.servers.map((server, currentIndex) =>
        currentIndex === index ? { ...server, [field]: value } : server,
      ),
    );
  };

  const addIntegration = () => {
    const normalizedKey = newIntegrationKey.trim();
    if (!normalizedKey) {
      toast.error("Integration key is required");
      return;
    }

    updateFormState("integrations", {
      ...formState.integrations,
      [normalizedKey]: newIntegrationValue.trim(),
    });
    setNewIntegrationKey("");
    setNewIntegrationValue("");
  };

  const removeIntegration = (key: string) => {
    const nextIntegrations = { ...formState.integrations };
    delete nextIntegrations[key];
    updateFormState("integrations", nextIntegrations);
  };

  return (
    <form onSubmit={handleSubmit} className="profile-form">
      <div className="settings-stack settings-stack--lg">
        <LlmProviderCard />

        <EmailConfigCard />

        <Card>
          <Card.Header>
            <h3 className="settings-section-title ui-section-title">Basic Information</h3>
          </Card.Header>
          <Card.Body>
            <div className="settings-stack settings-stack--md">
              <div className="form-group">
                <label htmlFor="display-name" className="form-label">
                  Display Name
                </label>
                <Input
                  id="display-name"
                  value={formState.displayName}
                  onChange={(e) => updateFormState("displayName", e.target.value)}
                  placeholder="Your name"
                />
                <small>How you'd like to be addressed</small>
              </div>

              <div className="form-group">
                <label htmlFor="role" className="form-label">
                  Role
                </label>
                <Input
                  id="role"
                  value={formState.role}
                  onChange={(e) => updateFormState("role", e.target.value)}
                  placeholder="e.g., software engineer, founder, student"
                />
                <small>Your professional role or position</small>
              </div>

              <div className="form-group">
                <label htmlFor="location" className="form-label">
                  Location
                </label>
                <Input
                  id="location"
                  value={formState.location}
                  onChange={(e) => updateFormState("location", e.target.value)}
                  placeholder="e.g., San Francisco, CA"
                />
                <small>Your location for timezone and regional context</small>
              </div>

              <div className="form-group">
                <label htmlFor="description" className="form-label">
                  Description
                </label>
                <textarea
                  id="description"
                  value={formState.description}
                  onChange={(e) => updateFormState("description", e.target.value)}
                  placeholder="Brief description of what you do and what you want AI to help with"
                  className="ui-input settings-textarea"
                  rows={3}
                />
                <small>What you do and how automations can help you</small>
              </div>
            </div>
          </Card.Body>
        </Card>

        <Card>
          <Card.Header>
            <h3 className="settings-section-title ui-section-title">Servers</h3>
            <Button type="button" onClick={addServer}>
              <PlusIcon /> Add Server
            </Button>
          </Card.Header>
          <Card.Body>
            <p className="section-description">
              Configure servers that automations can reference or SSH into
            </p>

            {formState.servers.length === 0 ? (
              <div className="settings-empty-state">
                <p>No servers configured</p>
              </div>
            ) : (
              <div className="servers-list">
                {formState.servers.map((server, index) => (
                  <Card key={index} variant="default" className="settings-card-muted">
                    <Card.Header>
                      <span className="server-index">Server {index + 1}</span>
                      <Button variant="danger" size="sm" onClick={() => removeServer(index)}>
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

        <Card>
          <Card.Header>
            <h3 className="settings-section-title ui-section-title">Integrations</h3>
          </Card.Header>
          <Card.Body>
            <p className="section-description">
              Tools and services you use (e.g., health_tracker: WHOOP, notes: Obsidian)
            </p>

            {Object.keys(formState.integrations).length === 0 ? (
              <div className="settings-empty-state settings-empty-state--spaced">
                <p>No integrations configured</p>
              </div>
            ) : (
              <div className="integrations-list">
                {Object.entries(formState.integrations).map(([key, value]) => (
                  <div key={key} className="integration-item">
                    <div className="integration-info">
                      <span className="integration-key">{key}:</span>
                      <span className="integration-value">{value}</span>
                    </div>
                    <Button variant="ghost" size="sm" onClick={() => removeIntegration(key)}>
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

        <Card>
          <Card.Header>
            <h3 className="settings-section-title ui-section-title">Oikos Tools</h3>
          </Card.Header>
          <Card.Body>
            <p className="section-description">
              Enable or disable tools that Oikos can use to help you
            </p>

            <div className="tools-list">
              {TOOL_DEFINITIONS.map((tool) => (
                <div key={tool.id} className="tool-toggle">
                  <label className="tool-label">
                    <input
                      type="checkbox"
                      checked={formState.tools[tool.id]}
                      onChange={(e) =>
                        updateFormState("tools", {
                          ...formState.tools,
                          [tool.id]: e.target.checked,
                        })
                      }
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

        <Card>
          <Card.Header>
            <h3 className="settings-section-title ui-section-title">Custom Instructions</h3>
          </Card.Header>
          <Card.Body>
            <p className="section-description">
              Specific preferences for how automations should respond to you
            </p>

            <div className="form-group">
              <textarea
                id="custom-instructions"
                value={formState.customInstructions}
                onChange={(e) => updateFormState("customInstructions", e.target.value)}
                placeholder="e.g., Always explain technical concepts in detail, prefer Python over JavaScript, etc."
                className="ui-input settings-textarea settings-textarea--lg"
                rows={4}
              />
            </div>
          </Card.Body>
        </Card>

        <div className="form-actions settings-form-actions">
          <Button type="button" variant="ghost" onClick={handleReset} disabled={isSaving}>
            Reset Changes
          </Button>
          <Button type="submit" variant="primary" size="lg" disabled={isSaving}>
            {isSaving ? "Saving..." : "Save Settings"}
          </Button>
        </div>
      </div>
    </form>
  );
}

export default function SettingsPage() {
  const queryClient = useQueryClient();

  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["user-context"],
    queryFn: getUserContext,
    refetchOnWindowFocus: false,
  });

  useReadinessFlag({ ready: !isLoading });

  const updateMutation = useMutation({
    mutationFn: updateUserContext,
    onSuccess: () => {
      toast.success("Settings saved successfully!");
      queryClient.invalidateQueries({ queryKey: ["user-context"] });
    },
    onError: (mutationError: Error) => {
      toast.error(`Failed to save settings: ${mutationError.message}`);
    },
  });

  const initialState = useMemo(() => createSettingsFormState(data?.context), [data?.context]);

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
        description="Configure your personal context so automations and assistants can better understand and help you."
      />

      <SettingsForm
        key={String(dataUpdatedAt)}
        initialState={initialState}
        isSaving={updateMutation.isPending}
        onSubmit={(context) => updateMutation.mutate(context)}
      />
    </PageShell>
  );
}
