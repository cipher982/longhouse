import { request } from "./base";

export interface SystemCapabilities {
  llm_available: boolean;
  auth_disabled: boolean;
}

/**
 * Fetch system capabilities for graceful degradation.
 * Returns which features are available based on server configuration.
 */
export async function fetchSystemCapabilities(): Promise<SystemCapabilities> {
  return request<SystemCapabilities>("/api/system/capabilities");
}
