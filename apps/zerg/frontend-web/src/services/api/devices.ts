/**
 * API functions for device token management.
 *
 * Device tokens authenticate CLI tools (like `longhouse ship`)
 * to this Longhouse instance.
 */

import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DeviceToken {
  id: string;
  device_id: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  is_valid: boolean;
}

export interface DeviceTokenCreate {
  device_id: string;
}

/** Returned only once during creation — includes the plain token. */
export interface DeviceTokenCreated {
  id: string;
  device_id: string;
  token: string;
  created_at: string;
}

export interface DeviceTokenList {
  tokens: DeviceToken[];
  total: number;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

export async function listDeviceTokens(includeRevoked = false): Promise<DeviceTokenList> {
  const qs = includeRevoked ? "?include_revoked=true" : "";
  return request<DeviceTokenList>(`/devices/tokens${qs}`);
}

export async function createDeviceToken(body: DeviceTokenCreate): Promise<DeviceTokenCreated> {
  return request<DeviceTokenCreated>("/devices/tokens", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function revokeDeviceToken(tokenId: string): Promise<void> {
  return request<void>(`/devices/tokens/${tokenId}`, {
    method: "DELETE",
  });
}
