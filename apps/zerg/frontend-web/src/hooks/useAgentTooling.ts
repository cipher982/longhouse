import { useMemo } from "react";
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
    queryKey: ["tooling", "container-policy"],
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

