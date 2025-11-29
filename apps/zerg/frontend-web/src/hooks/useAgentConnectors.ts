/**
 * React Query hooks for agent connector credentials API.
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
  fetchAgentConnectors,
  configureAgentConnector,
  testAgentConnectorBeforeSave,
  testAgentConnector,
  deleteAgentConnector,
} from "../services/api";

/**
 * Fetch all connector statuses for an agent.
 */
export function useAgentConnectors(agentId: number | null) {
  return useQuery<ConnectorStatus[]>({
    queryKey: ["agent", agentId, "connectors"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchAgentConnectors(agentId);
    },
    enabled: agentId != null,
  });
}

/**
 * Configure (create or update) connector credentials.
 */
export function useConfigureConnector(agentId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: ConnectorConfigureRequest) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return configureAgentConnector(agentId, payload);
    },
    onSuccess: () => {
      toast.success("Connector configured successfully");
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to configure connector: ${error.message}`);
    },
  });
}

/**
 * Test credentials before saving.
 */
export function useTestConnectorBeforeSave(agentId: number | null) {
  return useMutation({
    mutationFn: (payload: ConnectorTestRequest) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return testAgentConnectorBeforeSave(agentId, payload);
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
 * Test already-configured credentials.
 */
export function useTestConnector(agentId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectorType: string) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return testAgentConnector(agentId, connectorType);
    },
    onSuccess: (result: ConnectorTestResponse) => {
      if (result.success) {
        toast.success(`Test successful: ${result.message}`);
      } else {
        toast.error(`Test failed: ${result.message}`);
      }
      // Refresh connector list to show updated test status
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Test failed: ${error.message}`);
    },
  });
}

/**
 * Delete connector credentials.
 */
export function useDeleteConnector(agentId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectorType: string) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return deleteAgentConnector(agentId, connectorType);
    },
    onSuccess: () => {
      toast.success("Connector removed");
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove connector: ${error.message}`);
    },
  });
}
