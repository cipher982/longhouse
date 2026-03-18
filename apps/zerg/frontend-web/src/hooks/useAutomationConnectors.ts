/**
 * React Query hooks for automation connector credentials API.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  ConnectorStatus,
  ConnectorConfigureRequest,
  ConnectorTestRequest,
  ConnectorTestResponse,
} from "../types/connectors";
import {
  fetchAutomationConnectors,
  configureAutomationConnector,
  testAutomationConnectorBeforeSave,
} from "../services/api";

/**
 * Fetch all connector statuses for an automation.
 */
export function useAutomationConnectors(automationId: number | null) {
  return useQuery<ConnectorStatus[]>({
    queryKey: ["automation", automationId, "connectors"],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return fetchAutomationConnectors(automationId);
    },
    enabled: automationId != null,
  });
}

/**
 * Configure (create or update) connector credentials.
 */
export function useConfigureConnector(automationId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: ConnectorConfigureRequest) => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return configureAutomationConnector(automationId, payload);
    },
    onSuccess: () => {
      toast.success("Connector configured successfully");
      queryClient.invalidateQueries({ queryKey: ["automation", automationId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to configure connector: ${error.message}`);
    },
  });
}

/**
 * Test credentials before saving.
 */
export function useTestConnectorBeforeSave(automationId: number | null) {
  return useMutation({
    mutationFn: (payload: ConnectorTestRequest) => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return testAutomationConnectorBeforeSave(automationId, payload);
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
 * Test already-configured credentials and deletion are handled by the
 * automation connector API helpers directly when needed.
 */
