import { useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  Fiche,
  McpServerAddRequest,
  McpServerResponse,
  McpTestConnectionResponse,
  AvailableToolsResponse,
  ContainerPolicy,
} from "../services/api";
import {
  addMcpServer,
  fetchAvailableTools,
  fetchContainerPolicy,
  fetchFiche,
  fetchMcpServers,
  removeMcpServer,
  testMcpServer,
  updateFiche,
} from "../services/api";

export function useContainerPolicy() {
  return useQuery<ContainerPolicy>({
    queryKey: ["config", "container-policy"],
    queryFn: fetchContainerPolicy,
  });
}

export function useFicheDetails(ficheId: number | null) {
  return useQuery<Fiche>({
    queryKey: ["fiche", ficheId],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return fetchFiche(ficheId);
    },
    enabled: ficheId != null,
  });
}

export function useAvailableTools(ficheId: number | null) {
  return useQuery<AvailableToolsResponse>({
    queryKey: ["fiche", ficheId, "available-tools"],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return fetchAvailableTools(ficheId);
    },
    enabled: ficheId != null,
  });
}

export function useMcpServers(ficheId: number | null) {
  return useQuery<McpServerResponse[]>({
    queryKey: ["fiche", ficheId, "mcp-servers"],
    queryFn: () => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return fetchMcpServers(ficheId);
    },
    enabled: ficheId != null,
  });
}

export function useUpdateAllowedTools(ficheId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (allowedTools: string[] | null) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return updateFiche(ficheId, { allowed_tools: allowedTools ?? [] });
    },
    onSuccess: () => {
      toast.success("Allowed tools updated");
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to update tools: ${error.message}`);
    },
  });
}

export function useAddMcpServer(ficheId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: McpServerAddRequest) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return addMcpServer(ficheId, payload);
    },
    onSuccess: (_fiche) => {
      toast.success("MCP server added");
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "mcp-servers"] });
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "available-tools"] });
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to add MCP server: ${error.message}`);
    },
  });
}

export function useRemoveMcpServer(ficheId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (serverName: string) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return removeMcpServer(ficheId, serverName);
    },
    onSuccess: () => {
      toast.success("MCP server removed");
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "mcp-servers"] });
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId, "available-tools"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove MCP server: ${error.message}`);
    },
  });
}

export function useTestMcpServer(ficheId: number | null) {
  return useMutation({
    mutationFn: (payload: McpServerAddRequest) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return testMcpServer(ficheId, payload);
    },
    onSuccess: (result: McpTestConnectionResponse) => {
      const status = result.success ? "success" : "warn";
      if (status === "success") {
        toast.success(`Connection successful: ${result.tools?.length ?? 0} tool(s) available`);
      } else {
        toast.error(`Connection failed: ${result.message}`);
      }
    },
    onError: (error: Error) => {
      toast.error(`Connection test failed: ${error.message}`);
    },
  });
}

export function useToolOptions(ficheId: number | null) {
  const { data } = useAvailableTools(ficheId);
  return useMemo(() => {
    if (!data) {
      return [];
    }
    const builtin = data.builtin.map((name) => ({
      name,
      label: name,
      source: "builtin" as const,
    }));
    const mcpEntries = Object.entries(data.mcp).flatMap(([server, tools]) =>
      tools.map((tool) => ({
        name: tool,
        label: `${tool}`,
        source: `mcp:${server}` as const,
      }))
    );
    return [...builtin, ...mcpEntries];
  }, [data]);
}

/**
 * Hook for debounced auto-save mutations with queueing and rollback.
 * - Queues all user changes (never drops input)
 * - Fires queued changes after current mutation completes
 * - Collapses rapid consecutive calls within debounce window (500ms)
 * - Tracks last synced value for rollback on error
 */
export function useDebouncedUpdateAllowedTools(ficheId: number | null, debounceMs = 500) {
  const queryClient = useQueryClient();
  const debounceTimerRef = useRef<NodeJS.Timeout | null>(null);
  const pendingValueRef = useRef<string[] | null>(null);
  const lastSyncedRef = useRef<string[] | null>(null);

  const mutation = useMutation({
    mutationFn: (allowedTools: string[] | null) => {
      if (ficheId == null) {
        return Promise.reject(new Error("Missing fiche id"));
      }
      return updateFiche(ficheId, { allowed_tools: allowedTools ?? [] });
    },
    onSuccess: (response) => {
      // Track last successful sync as source of truth
      lastSyncedRef.current = response.allowed_tools ?? null;
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId] });

      // Fire queued change if one exists
      if (pendingValueRef.current !== null) {
        const queued = pendingValueRef.current;
        pendingValueRef.current = null;
        // Schedule on next tick to avoid nested mutation calls
        setTimeout(() => mutation.mutate(queued), 0);
      }
    },
    onError: (error: Error) => {
      toast.error(`Failed to update tools: ${error.message}. Changes reverted.`);
      // Force refresh from server to restore correct state
      queryClient.invalidateQueries({ queryKey: ["fiche", ficheId] });
      // Clear pending value on error to avoid retrying bad data
      pendingValueRef.current = null;
    },
  });

  const debouncedMutate = (allowedTools: string[] | null) => {
    // Always queue the latest value (never drop user input)
    pendingValueRef.current = allowedTools;

    // If mutation in-flight, queued value will fire in onSuccess
    if (mutation.isPending) {
      return; // Silently queue, don't show toast for every change
    }

    // Clear existing timer
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    // Set new timer
    debounceTimerRef.current = setTimeout(() => {
      if (pendingValueRef.current !== null) {
        mutation.mutate(pendingValueRef.current);
        pendingValueRef.current = null;
      }
      debounceTimerRef.current = null;
    }, debounceMs);
  };

  const flushPending = () => {
    // Immediately fire pending debounce
    if (debounceTimerRef.current && pendingValueRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
      mutation.mutate(pendingValueRef.current);
      pendingValueRef.current = null;
    }
  };

  const cancelPendingDebounce = () => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
      pendingValueRef.current = null;
    }
  };

  return {
    mutate: debouncedMutate,
    flush: flushPending,
    cancelPending: cancelPendingDebounce,
    isPending: mutation.isPending,
    isError: mutation.isError,
    hasPendingDebounce: debounceTimerRef.current !== null,
    lastSyncedValue: lastSyncedRef.current,
    error: mutation.error,
  };
}
