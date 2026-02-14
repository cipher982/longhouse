import { request } from "./base";

export interface SystemCapabilities {
  llm_available: boolean;
  auth_disabled: boolean;
}

/**
 * Fetch system capabilities for graceful degradation (legacy flat format).
 * Returns which features are available based on server configuration.
 */
export async function fetchSystemCapabilities(): Promise<SystemCapabilities> {
  return request<SystemCapabilities>("/system/capabilities");
}

// ---------------------------------------------------------------------------
// LLM Provider Configuration
// ---------------------------------------------------------------------------

export interface CapabilityStatus {
  available: boolean;
  source: string | null;
  provider_name: string | null;
  features: string[];
}

export interface LlmCapabilities {
  text: CapabilityStatus;
  embedding: CapabilityStatus;
}

export interface LlmProviderInfo {
  capability: string;
  provider_name: string;
  base_url: string | null;
  source: string;
  has_key: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface LlmProviderTestResult {
  success: boolean;
  message: string;
}

/** Fetch detailed LLM capability status (text + embedding). */
export async function fetchLlmCapabilities(): Promise<LlmCapabilities> {
  return request<LlmCapabilities>("/capabilities/llm");
}

/** List configured LLM providers for the current user. */
export async function fetchLlmProviders(): Promise<LlmProviderInfo[]> {
  return request<LlmProviderInfo[]>("/llm/providers");
}

/** Create or update an LLM provider config. */
export async function upsertLlmProvider(
  capability: string,
  data: { provider_name: string; api_key: string; base_url: string | null }
): Promise<{ success: boolean }> {
  return request<{ success: boolean }>(`/llm/providers/${capability}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

/** Remove an LLM provider config. */
export async function deleteLlmProvider(capability: string): Promise<void> {
  await request<void>(`/llm/providers/${capability}`, {
    method: "DELETE",
  });
}

/** Test an LLM provider connection before saving. */
export async function testLlmProvider(
  capability: string,
  data: { provider_name: string; api_key: string; base_url: string | null }
): Promise<LlmProviderTestResult> {
  return request<LlmProviderTestResult>(`/llm/providers/${capability}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}
