/**
 * React Query hooks for account-level connector credentials API.
 *
 * These hooks manage account-level integrations that are shared across
 * all agents owned by the user.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  AccountConnectorStatus,
  ConnectorConfigureRequest,
  ConnectorTestRequest,
  ConnectorTestResponse,
} from "../types/connectors";
import {
  fetchAccountConnectors,
  configureAccountConnector,
  testAccountConnectorBeforeSave,
  testAccountConnector,
  deleteAccountConnector,
} from "../services/api";

/**
 * Fetch all account-level connector statuses.
 */
export function useAccountConnectors() {
  return useQuery<AccountConnectorStatus[]>({
    queryKey: ["account", "connectors"],
    queryFn: fetchAccountConnectors,
  });
}

/**
 * Configure (create or update) account-level connector credentials.
 */
export function useConfigureAccountConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: ConnectorConfigureRequest) => {
      return configureAccountConnector(payload);
    },
    onSuccess: () => {
      toast.success("Integration configured successfully");
      queryClient.invalidateQueries({ queryKey: ["account", "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to configure integration: ${error.message}`);
    },
  });
}

/**
 * Test credentials before saving.
 */
export function useTestAccountConnectorBeforeSave() {
  return useMutation({
    mutationFn: (payload: ConnectorTestRequest) => {
      return testAccountConnectorBeforeSave(payload);
    },
    onSuccess: (result: ConnectorTestResponse) => {
      if (result.success) {
        toast.success(`Test successful: ${result.message}`);
      } else {
        toast.error(`Test failed: ${result.message}`);
      }
    },
    onError: (error: Error) => {
      toast.error(`Test failed: ${error.message}`);
    },
  });
}

/**
 * Test already-configured account-level credentials.
 */
export function useTestAccountConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectorType: string) => {
      return testAccountConnector(connectorType);
    },
    onSuccess: (result: ConnectorTestResponse) => {
      if (result.success) {
        toast.success(`Test successful: ${result.message}`);
      } else {
        toast.error(`Test failed: ${result.message}`);
      }
      // Refresh connector list to show updated test status
      queryClient.invalidateQueries({ queryKey: ["account", "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Test failed: ${error.message}`);
    },
  });
}

/**
 * Delete account-level connector credentials.
 */
export function useDeleteAccountConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectorType: string) => {
      return deleteAccountConnector(connectorType);
    },
    onSuccess: () => {
      toast.success("Integration removed");
      queryClient.invalidateQueries({ queryKey: ["account", "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove integration: ${error.message}`);
    },
  });
}
