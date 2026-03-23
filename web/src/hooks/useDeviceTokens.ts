/**
 * React Query hooks for device token management.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listDeviceTokens,
  createDeviceToken,
  revokeDeviceToken,
  type DeviceTokenList,
  type DeviceTokenCreated,
  type DeviceTokenCreate,
} from "../services/api/devices";
import toast from "react-hot-toast";

// ---------------------------------------------------------------------------
// Query Keys
// ---------------------------------------------------------------------------

export const deviceTokenKeys = {
  all: ["device-tokens"] as const,
  list: () => [...deviceTokenKeys.all, "list"] as const,
};

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useDeviceTokens() {
  return useQuery<DeviceTokenList, Error>({
    queryKey: deviceTokenKeys.list(),
    queryFn: () => listDeviceTokens(),
  });
}

export function useCreateDeviceToken() {
  const queryClient = useQueryClient();

  return useMutation<DeviceTokenCreated, Error, DeviceTokenCreate>({
    mutationFn: createDeviceToken,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: deviceTokenKeys.list() });
    },
    onError: (error) => {
      toast.error(error.message || "Failed to create device token");
    },
  });
}

export function useRevokeDeviceToken() {
  const queryClient = useQueryClient();

  return useMutation<void, Error, string>({
    mutationFn: revokeDeviceToken,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: deviceTokenKeys.list() });
      toast.success("Device token revoked");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to revoke token");
    },
  });
}
