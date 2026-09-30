/**
 * React Query hooks for device token management.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listDeviceTokens,
  createDeviceToken,
  revokeMachine,
  type MachineRevokeResult,
  type DeviceTokenList,
  type DeviceTokenCreated,
  type DeviceTokenCreate,
} from "@/shared/api/devices";
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

export function useRevokeMachine() {
  const queryClient = useQueryClient();

  return useMutation<MachineRevokeResult, Error, string>({
    mutationFn: revokeMachine,
    onSuccess: ({ device_id, revoked }) => {
      queryClient.invalidateQueries({ queryKey: deviceTokenKeys.list() });
      toast.success(
        revoked === 0
          ? `No valid tokens were left for ${device_id}`
          : `Revoked ${revoked} ${revoked === 1 ? "token" : "tokens"} for ${device_id}`,
      );
    },
    onError: (error) => {
      toast.error(error.message || "Failed to revoke machine");
    },
  });
}
