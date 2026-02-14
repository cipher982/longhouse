import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import {
  fetchLlmCapabilities,
  fetchLlmProviders,
  upsertLlmProvider,
  deleteLlmProvider,
  testLlmProvider,
  type LlmCapabilities,
  type LlmProviderInfo,
} from "../services/api/system";
import { Badge, Button, Card, Input, Spinner } from "./ui";

// Known providers with their default base URLs
const KNOWN_PROVIDERS = [
  { id: "openai", name: "OpenAI", baseUrl: "" },
  { id: "groq", name: "Groq", baseUrl: "https://api.groq.com/openai/v1" },
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
  dbProviders,
  onConfigure,
  onDelete,
  isConfiguring,
}: {
  capability: Capability;
  label: string;
  status: { available: boolean; source: string | null; provider_name: string | null; features: string[] };
  dbProviders: LlmProviderInfo[];
  onConfigure: (cap: Capability) => void;
  onDelete: (cap: Capability) => void;
  isConfiguring: Capability | null;
}) {
  const dbConfig = dbProviders.find((p) => p.capability === capability);
  const isActive = status.available;
  // Derive source/provider from authenticated provider list (not public endpoint)
  const sourceLabel = dbConfig ? "DB" : isActive ? "Env" : null;
  const providerName = dbConfig?.provider_name ?? null;

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
        <span className="llm-capability-features">
          Enables: {status.features.join(", ")}
        </span>
      </div>
      <div className="llm-capability-actions">
        {dbConfig && (
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
    queryKey: ["llm-providers"],
    queryFn: fetchLlmProviders,
  });

  const saveMutation = useMutation({
    mutationFn: ({
      capability,
      data,
    }: {
      capability: string;
      data: { provider_name: string; api_key: string; base_url: string | null };
    }) => upsertLlmProvider(capability, data),
    onSuccess: () => {
      toast.success("Provider saved");
      queryClient.invalidateQueries({ queryKey: ["llm-capabilities"] });
      queryClient.invalidateQueries({ queryKey: ["llm-providers"] });
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
    },
    onError: (err: Error) => {
      toast.error(`Failed to remove: ${err.message}`);
    },
  });

  function resetForm() {
    setForm({ providerName: "openai", apiKey: "", baseUrl: "" });
    setTestResult(null);
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
    } else {
      resetForm();
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
    if (!configuringCap || !form.apiKey) return;
    setIsTesting(true);
    setTestResult(null);
    try {
      const result = await testLlmProvider(configuringCap, {
        provider_name: form.providerName,
        api_key: form.apiKey,
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
    if (!configuringCap || !form.apiKey) return;
    saveMutation.mutate({
      capability: configuringCap,
      data: {
        provider_name: form.providerName,
        api_key: form.apiKey,
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

  return (
    <Card>
      <Card.Header>
        <h3 className="settings-section-title ui-section-title">LLM Providers</h3>
      </Card.Header>
      <Card.Body>
        <p className="section-description">
          Configure LLM providers for AI-powered features. Keys are encrypted at rest.
        </p>

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
                  dbProviders={providers}
                  onConfigure={handleConfigure}
                  onDelete={handleDelete}
                  isConfiguring={configuringCap}
                />
                <CapabilityRow
                  capability="embedding"
                  label="Embeddings"
                  status={capabilities.embedding}
                  dbProviders={providers}
                  onConfigure={handleConfigure}
                  onDelete={handleDelete}
                  isConfiguring={configuringCap}
                />
              </>
            )}

            {configuringCap && (
              <div className="llm-config-form">
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
                    <label className="form-label">API Key</label>
                    <Input
                      type="password"
                      value={form.apiKey}
                      onChange={(e) => {
                        setForm({ ...form, apiKey: e.target.value });
                        setTestResult(null);
                      }}
                      placeholder="sk-..."
                    />
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

                {testResult && (
                  <div
                    className={`llm-test-result ${testResult.success ? "llm-test-result--success" : "llm-test-result--error"}`}
                  >
                    {testResult.message}
                  </div>
                )}

                <div className="llm-config-actions">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleTest}
                    disabled={!form.apiKey || isTesting}
                  >
                    {isTesting ? "Testing..." : "Test Connection"}
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={handleSave}
                    disabled={!form.apiKey || saveMutation.isPending}
                  >
                    {saveMutation.isPending ? "Saving..." : "Save"}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </Card.Body>
    </Card>
  );
}
