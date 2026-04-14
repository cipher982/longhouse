import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import {
  fetchLlmCapabilities,
  fetchEffectiveLlmProviders,
  upsertLlmProvider,
  deleteLlmProvider,
  testLlmProvider,
  type LlmCapabilities,
  type LlmProviderInfo,
} from "../services/api/system";
import { Badge, Button, Card, Input, Spinner } from "./ui";

// Known providers with their default base URLs
const KNOWN_PROVIDERS = [
  { id: "openrouter", name: "OpenRouter (recommended)", baseUrl: "https://openrouter.ai/api/v1" },
  { id: "openai", name: "OpenAI", baseUrl: "" },
  { id: "groq", name: "Groq", baseUrl: "https://api.groq.com/openai/v1" },
  { id: "xai", name: "xAI", baseUrl: "https://api.x.ai/v1" },
  { id: "ollama", name: "Ollama / Local", baseUrl: "http://localhost:11434/v1" },
  { id: "custom", name: "Custom", baseUrl: "" },
] as const;

type Capability = "text" | "embedding";

interface ConfigFormState {
  providerName: string;
  apiKey: string;
  baseUrl: string;
}

function CapabilityRow({
  capability,
  label,
  status,
  providers,
  onConfigure,
  onDelete,
  isConfiguring,
  isSharedPool,
}: {
  capability: Capability;
  label: string;
  status: { available: boolean; source: string | null; provider_name: string | null; features: string[] };
  providers: LlmProviderInfo[];
  onConfigure: (cap: Capability) => void;
  onDelete: (cap: Capability) => void;
  isConfiguring: Capability | null;
  isSharedPool: boolean;
}) {
  const effectiveConfig = providers.find((p) => p.capability === capability);
  const isDbOverride = effectiveConfig?.source === "database";
  const isActive = status.available;
  const sourceLabel = effectiveConfig
    ? effectiveConfig.source === "database"
      ? "Custom"
      : isSharedPool
        ? "Shared"
        : "Platform"
    : null;
  const providerName = effectiveConfig?.provider_name ?? null;

  return (
    <div className="llm-capability-row">
      <div className="llm-capability-info">
        <div className="llm-capability-header">
          <span className="llm-capability-label">{label}</span>
          <Badge variant={isActive ? "success" : "warning"}>
            {isActive ? "Active" : "Not configured"}
          </Badge>
          {sourceLabel && (
            <span className="llm-capability-source">{sourceLabel}</span>
          )}
        </div>
        {isActive && providerName && (
          <span className="llm-capability-provider">{providerName}</span>
        )}
        {isActive && isSharedPool && !providerName && (
          <span className="llm-capability-provider">Shared instance key</span>
        )}
        <span className="llm-capability-features">
          Enables: {status.features.join(", ")}
        </span>
      </div>
      <div className="llm-capability-actions">
        {isDbOverride && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDelete(capability)}
          >
            Remove
          </Button>
        )}
        <Button
          variant={isConfiguring === capability ? "secondary" : "primary"}
          size="sm"
          onClick={() => onConfigure(capability)}
        >
          {isConfiguring === capability ? "Cancel" : "Configure"}
        </Button>
      </div>
    </div>
  );
}

export default function LlmProviderCard() {
  const queryClient = useQueryClient();
  const [configuringCap, setConfiguringCap] = useState<Capability | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [form, setForm] = useState<ConfigFormState>({
    providerName: "openai",
    apiKey: "",
    baseUrl: "",
  });
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [isTesting, setIsTesting] = useState(false);

  const { data: capabilities, isLoading: capLoading, error: capError } = useQuery({
    queryKey: ["llm-capabilities"],
    queryFn: fetchLlmCapabilities,
  });

  const { data: providers = [], isLoading: provLoading, error: provError } = useQuery({
    queryKey: ["llm-providers-effective"],
    queryFn: fetchEffectiveLlmProviders,
  });

  const saveMutation = useMutation({
    mutationFn: ({
      capability,
      data,
    }: {
      capability: string;
      data: { provider_name: string; api_key?: string | null; base_url: string | null };
    }) => upsertLlmProvider(capability, data),
    onSuccess: () => {
      toast.success("Provider saved");
      queryClient.invalidateQueries({ queryKey: ["llm-capabilities"] });
      queryClient.invalidateQueries({ queryKey: ["llm-providers"] });
      queryClient.invalidateQueries({ queryKey: ["llm-providers-effective"] });
      setConfiguringCap(null);
      resetForm();
    },
    onError: (err: Error) => {
      toast.error(`Failed to save: ${err.message}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (capability: string) => deleteLlmProvider(capability),
    onSuccess: () => {
      toast.success("Provider removed");
      queryClient.invalidateQueries({ queryKey: ["llm-capabilities"] });
      queryClient.invalidateQueries({ queryKey: ["llm-providers"] });
      queryClient.invalidateQueries({ queryKey: ["llm-providers-effective"] });
    },
    onError: (err: Error) => {
      toast.error(`Failed to remove: ${err.message}`);
    },
  });

  function resetForm() {
    setForm({ providerName: "openai", apiKey: "", baseUrl: "" });
    setTestResult(null);
    setIsEditing(false);
  }

  function handleConfigure(cap: Capability) {
    if (configuringCap === cap) {
      setConfiguringCap(null);
      resetForm();
      return;
    }
    // Pre-fill from existing config if available
    const existing = providers.find((p) => p.capability === cap);
    if (existing) {
      setForm({
        providerName: existing.provider_name,
        apiKey: "",
        baseUrl: existing.base_url || "",
      });
      setIsEditing(false);
    } else {
      resetForm();
      setIsEditing(true);
    }
    setConfiguringCap(cap);
    setTestResult(null);
  }

  function handleProviderSelect(providerId: string) {
    const known = KNOWN_PROVIDERS.find((p) => p.id === providerId);
    setForm({
      ...form,
      providerName: providerId,
      // Always reset base URL to the known provider's default.
      // For "custom", keep whatever is already there.
      baseUrl: providerId === "custom" ? form.baseUrl : (known?.baseUrl ?? ""),
    });
    setTestResult(null);
  }

  async function handleTest() {
    if (!configuringCap) return;
    setIsTesting(true);
    setTestResult(null);
    try {
      const result = await testLlmProvider(configuringCap, {
        provider_name: form.providerName,
        api_key: form.apiKey || null,
        base_url: form.baseUrl || null,
      });
      setTestResult(result);
    } catch {
      setTestResult({ success: false, message: "Request failed" });
    } finally {
      setIsTesting(false);
    }
  }

  function handleSave() {
    if (!configuringCap) return;
    const existing = providers.find((p) => p.capability === configuringCap);
    const isDbOverride = existing?.source === "database";
    if (!isDbOverride && !form.apiKey) {
      toast.error("Enter an API key to create an override");
      return;
    }
    saveMutation.mutate({
      capability: configuringCap,
      data: {
        provider_name: form.providerName,
        api_key: form.apiKey || null,
        base_url: form.baseUrl || null,
      },
    });
  }

  function handleDelete(cap: Capability) {
    if (confirm(`Remove ${cap} provider config? Will fall back to environment variable.`)) {
      deleteMutation.mutate(cap);
    }
  }

  const isLoading = capLoading || provLoading;
  const fetchError = capError || provError;
  const hasTextConfig = providers.some((p) => p.capability === "text" && p.source === "database");
  const hasEmbeddingConfig = providers.some((p) => p.capability === "embedding" && p.source === "database");
  const sharedTextPool = Boolean(capabilities?.text.available && !hasTextConfig);
  const sharedEmbeddingPool = Boolean(capabilities?.embedding.available && !hasEmbeddingConfig);
  const configuringProvider = configuringCap
    ? providers.find((p) => p.capability === configuringCap) ?? null
    : null;
  const configuringUsesStoredKey = configuringProvider?.source === "database";
  const hasCurrentKeyPreview = Boolean(configuringProvider?.api_key_preview);
  const canTestCurrentConfig = Boolean(form.apiKey || hasCurrentKeyPreview);
  const currentApiKeyPreview = configuringProvider?.api_key_preview ?? "Not configured";
  const currentProviderName = configuringProvider?.provider_name ?? "Not configured";
  const currentBaseUrl = configuringProvider?.base_url || "Provider default";

  return (
    <Card>
      <Card.Header>
        <h3 className="settings-section-title ui-section-title">LLM Providers</h3>
      </Card.Header>
      <Card.Body>
        <p className="section-description">
          Configure LLM providers for AI-powered features. Keys are encrypted at rest.
        </p>

        {sharedTextPool && (
          <div className="llm-shared-banner">
            <div className="llm-shared-title">Shared LLM pool active</div>
            <div className="llm-shared-body">
              This instance is using a shared API key for text models. Limits may apply.
              Add your own key to remove shared limits and use higher tiers.
            </div>
          </div>
        )}

        {fetchError ? (
          <div className="llm-test-result llm-test-result--error">
            Failed to load provider status. {String(fetchError)}
          </div>
        ) : isLoading ? (
          <div className="llm-loading">
            <Spinner size="sm" />
          </div>
        ) : (
          <div className="llm-capabilities-list">
            {capabilities && (
              <>
                <CapabilityRow
                  capability="text"
                  label="Text LLM"
                  status={capabilities.text}
                  providers={providers}
                  onConfigure={handleConfigure}
                  onDelete={handleDelete}
                  isConfiguring={configuringCap}
                  isSharedPool={sharedTextPool}
                />
                <CapabilityRow
                  capability="embedding"
                  label="Embeddings"
                  status={capabilities.embedding}
                  providers={providers}
                  onConfigure={handleConfigure}
                  onDelete={handleDelete}
                  isConfiguring={configuringCap}
                  isSharedPool={sharedEmbeddingPool}
                />
              </>
            )}

            {configuringCap && (
              <div className="llm-config-form">
                <div className="llm-config-fields">
                  <div className="form-group">
                    <label className="form-label">Current Provider</label>
                    <Input
                      value={currentProviderName}
                      readOnly
                    />
                    <small>Effective provider currently in use</small>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Current API Key</label>
                    <Input
                      value={currentApiKeyPreview}
                      readOnly
                    />
                    <small>
                      {configuringProvider?.api_key_preview
                        ? "Masked preview of the configured API key."
                        : "No API key configured."}
                    </small>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Current Base URL</label>
                    <Input
                      value={currentBaseUrl}
                      readOnly
                    />
                    <small>Effective base URL currently in use</small>
                  </div>
                </div>

                {testResult && (
                  <div
                    className={`llm-test-result ${testResult.success ? "llm-test-result--success" : "llm-test-result--error"}`}
                  >
                    {testResult.message}
                  </div>
                )}

                {!isEditing && (
                  <div className="llm-config-actions">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={handleTest}
                      disabled={!canTestCurrentConfig || isTesting}
                    >
                      {isTesting ? "Testing..." : hasCurrentKeyPreview && !form.apiKey ? "Test Current Config" : "Test Connection"}
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setIsEditing(true)}
                    >
                      {configuringProvider ? "Edit Settings" : "Add Settings"}
                    </Button>
                  </div>
                )}

                {isEditing && (
                  <>
                    <div className="llm-test-result">
                      {configuringProvider
                        ? "Update provider details below. Enter a new API key only if you want to replace the current one."
                        : "Add your own provider key to enable this capability."}
                    </div>

                    <div className="llm-provider-select">
                      {KNOWN_PROVIDERS.map((p) => (
                        <label key={p.id} className="llm-provider-option">
                          <input
                            type="radio"
                            name="provider"
                            value={p.id}
                            checked={form.providerName === p.id}
                            onChange={() => handleProviderSelect(p.id)}
                          />
                          <span>{p.name}</span>
                        </label>
                      ))}
                    </div>

                    <div className="llm-config-fields">
                      <div className="form-group">
                        <label className="form-label">New API Key</label>
                        <Input
                          type="password"
                          value={form.apiKey}
                          onChange={(e) => {
                            setForm({ ...form, apiKey: e.target.value });
                            setTestResult(null);
                          }}
                          placeholder={
                            configuringProvider?.api_key_preview
                              ? "Replace current API key"
                              : "sk-..."
                          }
                        />
                        <small>
                          {configuringProvider?.api_key_preview
                            ? "Leave empty to keep the current API key."
                            : "Required when adding a provider override."}
                        </small>
                      </div>

                      <div className="form-group">
                        <label className="form-label">Base URL</label>
                        <Input
                          value={form.baseUrl}
                          onChange={(e) => {
                            setForm({ ...form, baseUrl: e.target.value });
                            setTestResult(null);
                          }}
                          placeholder={form.providerName === "openai" ? "Default (api.openai.com)" : "https://..."}
                        />
                        <small>Leave empty for provider default</small>
                      </div>
                    </div>

                    <div className="llm-config-actions">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setIsEditing(false);
                          if (configuringProvider) {
                            setForm({
                              providerName: configuringProvider.provider_name,
                              apiKey: "",
                              baseUrl: configuringProvider.base_url || "",
                            });
                          } else {
                            resetForm();
                          }
                        }}
                      >
                        Stop Editing
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={handleTest}
                        disabled={!canTestCurrentConfig || isTesting}
                      >
                        {isTesting ? "Testing..." : hasCurrentKeyPreview && !form.apiKey ? "Test Current Config" : "Test Connection"}
                      </Button>
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={handleSave}
                        disabled={saveMutation.isPending || (!configuringUsesStoredKey && !form.apiKey)}
                      >
                        {saveMutation.isPending ? "Saving..." : "Save"}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </Card.Body>
    </Card>
  );
}
