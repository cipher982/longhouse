import { useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  Agent,
  McpServerAddRequest,
  McpServerResponse,
  McpTestConnectionResponse,
  AvailableToolsResponse,
  ContainerPolicy,
} from "../services/api";
import {
  addMcpServer,
  fetchAgent,
  fetchAvailableTools,
  fetchContainerPolicy,
  fetchMcpServers,
  removeMcpServer,
  testMcpServer,
  updateAgent,
} from "../services/api";

export function useContainerPolicy() {
  return useQuery<ContainerPolicy>({
    queryKey: ["config", "container-policy"],
    queryFn: fetchContainerPolicy,
  });
}

export function useAgentDetails(agentId: number | null) {
  return useQuery<Agent>({
    queryKey: ["agent", agentId],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchAgent(agentId);
    },
    enabled: agentId != null,
  });
}

export function useAvailableTools(agentId: number | null) {
  return useQuery<AvailableToolsResponse>({
    queryKey: ["agent", agentId, "available-tools"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchAvailableTools(agentId);
    },
    enabled: agentId != null,
  });
}

export function useMcpServers(agentId: number | null) {
  return useQuery<McpServerResponse[]>({
    queryKey: ["agent", agentId, "mcp-servers"],
    queryFn: () => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return fetchMcpServers(agentId);
    },
    enabled: agentId != null,
  });
}

export function useUpdateAllowedTools(agentId: number | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (allowedTools: string[] | null) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return updateAgent(agentId, { allowed_tools: allowedTools ?? [] });
    },
    onSuccess: () => {
      toast.success("Allowed tools updated");
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to update tools: ${error.message}`);
    },
  });
}

export function useAddMcpServer(agentId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: McpServerAddRequest) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return addMcpServer(agentId, payload);
    },
    onSuccess: (_agent) => {
      toast.success("MCP server added");
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "mcp-servers"] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "available-tools"] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to add MCP server: ${error.message}`);
    },
  });
}

export function useRemoveMcpServer(agentId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (serverName: string) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return removeMcpServer(agentId, serverName);
    },
    onSuccess: () => {
      toast.success("MCP server removed");
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "mcp-servers"] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "available-tools"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove MCP server: ${error.message}`);
    },
  });
}

export function useTestMcpServer(agentId: number | null) {
  return useMutation({
    mutationFn: (payload: McpServerAddRequest) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return testMcpServer(agentId, payload);
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

export function useToolOptions(agentId: number | null) {
  const { data } = useAvailableTools(agentId);
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
export function useDebouncedUpdateAllowedTools(agentId: number | null, debounceMs = 500) {
  const queryClient = useQueryClient();
  const debounceTimerRef = useRef<NodeJS.Timeout | null>(null);
  const pendingValueRef = useRef<string[] | null>(null);
  const lastSyncedRef = useRef<string[] | null>(null);

  const mutation = useMutation({
    mutationFn: (allowedTools: string[] | null) => {
      if (agentId == null) {
        return Promise.reject(new Error("Missing agent id"));
      }
      return updateAgent(agentId, { allowed_tools: allowedTools ?? [] });
    },
    onSuccess: (response) => {
      // Track last successful sync as source of truth
      lastSyncedRef.current = response.allowed_tools ?? null;
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });

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
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
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

