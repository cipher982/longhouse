/**
 * React Query hooks for fiche connector credentials API.
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
  fetchFicheConnectors,
  configureFicheConnector,
  testFicheConnectorBeforeSave,
  testFicheConnector,
  deleteFicheConnector,
} from "../services/api";

/**
 * Fetch all connector statuses for a fiche.
 */
export function useFicheConnectors(ficheId: number | null) {
  return useQuery<ConnectorStatus[]>({
    queryKey: ["fiche", ficheId, "connectors"],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return fetchFicheConnectors(ficheId);
    },
    enabled: ficheId != null,
  });
}

/**
 * Configure (create or update) connector credentials.
 */
export function useConfigureConnector(ficheId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: ConnectorConfigureRequest) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return configureFicheConnector(ficheId, payload);
    },
    onSuccess: () => {
      toast.success("Connector configured successfully");
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to configure connector: ${error.message}`);
    },
  });
}

/**
 * Test credentials before saving.
 */
export function useTestConnectorBeforeSave(ficheId: number | null) {
  return useMutation({
    mutationFn: (payload: ConnectorTestRequest) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return testFicheConnectorBeforeSave(ficheId, payload);
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
export function useTestConnector(ficheId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectorType: string) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return testFicheConnector(ficheId, connectorType);
    },
    onSuccess: (result: ConnectorTestResponse) => {
      if (result.success) {
        toast.success(`Test successful: ${result.message}`);
      } else {
        toast.error(`Test failed: ${result.message}`);
      }
      // Refresh connector list to show updated test status
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Test failed: ${error.message}`);
    },
  });
}

/**
 * Delete connector credentials.
 */
export function useDeleteConnector(ficheId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectorType: string) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return deleteFicheConnector(ficheId, connectorType);
    },
    onSuccess: () => {
      toast.success("Connector removed");
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "connectors"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove connector: ${error.message}`);
    },
  });
}
