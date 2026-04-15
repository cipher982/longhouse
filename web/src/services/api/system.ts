import { request } from "./base";
import type { components } from "../../generated/openapi-types";

type Schemas = components["schemas"];
type ApiCapabilityStatus = Schemas["CapabilityStatus"];
type ApiLlmCapabilities = Schemas["CapabilitiesResponse"];
type ApiLlmProviderInfo = Schemas["LlmProviderInfo"];
type ApiLlmProviderTestResult = Schemas["LlmProviderTestResponse"];

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

export interface CapabilityStatus extends Omit<ApiCapabilityStatus, "source" | "provider_name"> {
  source: string | null;
  provider_name: string | null;
}

export interface LlmCapabilities extends Omit<ApiLlmCapabilities, "text" | "embedding"> {
  text: CapabilityStatus;
  embedding: CapabilityStatus;
}

export type LlmProviderInfo = ApiLlmProviderInfo;
export type LlmProviderTestResult = ApiLlmProviderTestResult;

/** Fetch detailed LLM capability status (text + embedding). */
export async function fetchLlmCapabilities(): Promise<LlmCapabilities> {
  return request<LlmCapabilities>("/capabilities/llm");
}

/** List configured LLM providers for the current user. */
export async function fetchLlmProviders(): Promise<LlmProviderInfo[]> {
  return request<LlmProviderInfo[]>("/llm/providers");
}

/** List effective LLM providers for the current user, including env-backed defaults. */
export async function fetchEffectiveLlmProviders(): Promise<LlmProviderInfo[]> {
  return request<LlmProviderInfo[]>("/llm/providers/effective");
}

/** Create or update an LLM provider config. */
export async function upsertLlmProvider(
  capability: string,
  data: { provider_name: string; api_key?: string | null; base_url: string | null }
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
  data: { provider_name: string; api_key?: string | null; base_url: string | null }
): Promise<LlmProviderTestResult> {
  return request<LlmProviderTestResult>(`/llm/providers/${capability}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}
