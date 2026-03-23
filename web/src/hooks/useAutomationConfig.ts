import { useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  Automation,
  McpServerAddRequest,
  McpServerResponse,
  McpTestConnectionResponse,
  AvailableToolsResponse,
  ContainerPolicy,
} from "../services/api";
import {
  addMcpServer,
  fetchAutomation,
  fetchAutomationAvailableTools,
  fetchContainerPolicy,
  fetchAutomationMcpServers,
  removeMcpServer,
  testMcpServer,
  updateAutomation,
} from "../services/api";

type AllowedToolsMutationCallbacks = {
  onSuccess?: (allowedTools: string[] | null) => void;
  onError?: (error: Error) => void;
};

export function useContainerPolicy() {
  return useQuery<ContainerPolicy>({
    queryKey: ["config", "container-policy"],
    queryFn: fetchContainerPolicy,
  });
}

export function useAutomationDetails(automationId: number | null) {
  return useQuery<Automation>({
    queryKey: ["automation", automationId],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return fetchAutomation(automationId);
    },
    enabled: automationId != null,
  });
}

export function useAvailableTools(automationId: number | null) {
  return useQuery<AvailableToolsResponse>({
    queryKey: ["automation", automationId, "available-tools"],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return fetchAutomationAvailableTools(automationId);
    },
    enabled: automationId != null,
  });
}

export function useMcpServers(automationId: number | null) {
  return useQuery<McpServerResponse[]>({
    queryKey: ["automation", automationId, "mcp-servers"],
    queryFn: () => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return fetchAutomationMcpServers(automationId);
    },
    enabled: automationId != null,
  });
}
export function useAddMcpServer(automationId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: McpServerAddRequest) => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return addMcpServer(automationId, payload);
    },
    onSuccess: () => {
      toast.success("MCP server added");
      queryClient.invalidateQueries({ queryKey: ["automation", automationId, "mcp-servers"] });
      queryClient.invalidateQueries({ queryKey: ["automation", automationId, "available-tools"] });
      queryClient.invalidateQueries({ queryKey: ["automation", automationId] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to add MCP server: ${error.message}`);
    },
  });
}

export function useRemoveMcpServer(automationId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (serverName: string) => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return removeMcpServer(automationId, serverName);
    },
    onSuccess: () => {
      toast.success("MCP server removed");
      queryClient.invalidateQueries({ queryKey: ["automation", automationId, "mcp-servers"] });
      queryClient.invalidateQueries({ queryKey: ["automation", automationId, "available-tools"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove MCP server: ${error.message}`);
    },
  });
}

export function useTestMcpServer(automationId: number | null) {
  return useMutation({
    mutationFn: (payload: McpServerAddRequest) => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return testMcpServer(automationId, payload);
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

export function useToolOptions(automationId: number | null) {
  const { data } = useAvailableTools(automationId);
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
export function useDebouncedUpdateAllowedTools(automationId: number | null, debounceMs = 500) {
  const queryClient = useQueryClient();
  const debounceTimerRef = useRef<NodeJS.Timeout | null>(null);
  const pendingValueRef = useRef<string[] | null>(null);
  const pendingCallbacksRef = useRef<AllowedToolsMutationCallbacks | null>(null);
  const activeCallbacksRef = useRef<AllowedToolsMutationCallbacks | null>(null);
  const lastSyncedRef = useRef<string[] | null>(null);

  const mutation = useMutation({
    mutationFn: (allowedTools: string[] | null) => {
      if (automationId == null) {
        return Promise.reject(new Error("Missing automation id"));
      }
      return updateAutomation(automationId, { allowed_tools: allowedTools ?? [] });
    },
    onSuccess: (response) => {
      // Track last successful sync as source of truth
      lastSyncedRef.current = response.allowed_tools ?? null;
      activeCallbacksRef.current?.onSuccess?.(lastSyncedRef.current);
      activeCallbacksRef.current = null;
      queryClient.invalidateQueries({ queryKey: ["automation", automationId] });

      // Fire queued change if one exists
      if (pendingValueRef.current !== null) {
        const queued = pendingValueRef.current;
        const queuedCallbacks = pendingCallbacksRef.current;
        pendingValueRef.current = null;
        pendingCallbacksRef.current = null;
        // Schedule on next tick to avoid nested mutation calls
        setTimeout(() => {
          activeCallbacksRef.current = queuedCallbacks;
          mutation.mutate(queued);
        }, 0);
      }
    },
    onError: (error: Error) => {
      toast.error(`Failed to update tools: ${error.message}. Changes reverted.`);
      activeCallbacksRef.current?.onError?.(error);
      activeCallbacksRef.current = null;
      // Force refresh from server to restore correct state
      queryClient.invalidateQueries({ queryKey: ["automation", automationId] });
      // Clear pending value on error to avoid retrying bad data
      pendingValueRef.current = null;
      pendingCallbacksRef.current = null;
    },
  });

  const debouncedMutate = (allowedTools: string[] | null, callbacks?: AllowedToolsMutationCallbacks) => {
    // Always queue the latest value (never drop user input)
    pendingValueRef.current = allowedTools;
    pendingCallbacksRef.current = callbacks ?? null;

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
        activeCallbacksRef.current = pendingCallbacksRef.current;
        pendingCallbacksRef.current = null;
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
      activeCallbacksRef.current = pendingCallbacksRef.current;
      pendingCallbacksRef.current = null;
      mutation.mutate(pendingValueRef.current);
      pendingValueRef.current = null;
    }
  };

  const cancelPendingDebounce = () => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
      pendingValueRef.current = null;
      pendingCallbacksRef.current = null;
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
