import { request, buildUrl } from "./base";
import type {
  Agent,
  AgentSummary,
  AgentCreatePayload,
  AgentUpdatePayload,
  AgentRun,
  DashboardSnapshot,
  ContainerPolicy,
  AvailableToolsResponse,
  McpServerAddRequest,
  McpServerResponse,
  McpTestConnectionResponse,
} from "./types";

type FetchAgentsParams = {
  scope?: "my" | "all";
  limit?: number;
  skip?: number;
};

type DashboardRunsBundleResponse = {
  agent_id: number;
  runs: AgentRun[];
};

type DashboardSnapshotResponse = {
  scope: "my" | "all";
  fetched_at: string;
  runs_limit: number;
  agents: AgentSummary[];
  runs: DashboardRunsBundleResponse[];
};

type FetchDashboardParams = {
  scope?: "my" | "all";
  runsLimit?: number;
  skip?: number;
  limit?: number;
};

type RunAgentResponse = {
  thread_id: number;
};

export async function fetchAgents(params: FetchAgentsParams = {}): Promise<AgentSummary[]> {
  const scope = params.scope ?? "my";
  const limit = params.limit ?? 100;
  const skip = params.skip ?? 0;
  const searchParams = new URLSearchParams({
    scope,
    limit: String(limit),
    skip: String(skip),
  });

  return request<AgentSummary[]>(`/agents?${searchParams.toString()}`);
}

export async function fetchDashboardSnapshot(params: FetchDashboardParams = {}): Promise<DashboardSnapshot> {
  const scope = params.scope ?? "my";
  const runsLimit = params.runsLimit ?? 50;
  const limit = params.limit;
  const skip = params.skip;

  const searchParams = new URLSearchParams({
    scope,
    runs_limit: String(runsLimit),
  });

  if (typeof limit === "number") {
    searchParams.set("limit", String(limit));
  }
  if (typeof skip === "number") {
    searchParams.set("skip", String(skip));
  }

  const response = await request<DashboardSnapshotResponse>(`/agents/dashboard?${searchParams.toString()}`);
  return {
    scope: response.scope,
    fetchedAt: response.fetched_at,
    runsLimit: response.runs_limit,
    agents: response.agents,
    runs: response.runs.map((bundle) => ({
      agentId: bundle.agent_id,
      runs: bundle.runs,
    })),
  };
}

export async function createAgent(payload: AgentCreatePayload): Promise<Agent> {
  return request<Agent>(`/agents`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchAgent(agentId: number): Promise<Agent> {
  return request<Agent>(`/agents/${agentId}`);
}

export async function updateAgent(agentId: number, payload: AgentUpdatePayload): Promise<Agent> {
  return request<Agent>(`/agents/${agentId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function resetAgent(agentId: number): Promise<Agent> {
  return updateAgent(agentId, { status: "idle", last_error: "" });
}

export async function runAgent(agentId: number): Promise<RunAgentResponse> {
  return request<RunAgentResponse>(`/agents/${agentId}/task`, {
    method: "POST",
  });
}

export async function fetchAgentRuns(agentId: number, limit = 20): Promise<AgentRun[]> {
  return request<AgentRun[]>(`/agents/${agentId}/runs?limit=${limit}`);
}

export async function fetchContainerPolicy(): Promise<ContainerPolicy> {
  return request<ContainerPolicy>(`/config/container-policy`);
}

export async function fetchAvailableTools(agentId: number): Promise<AvailableToolsResponse> {
  return request<AvailableToolsResponse>(`/agents/${agentId}/mcp-servers/available-tools`);
}

export async function fetchMcpServers(agentId: number): Promise<McpServerResponse[]> {
  return request<McpServerResponse[]>(`/agents/${agentId}/mcp-servers/`);
}

export async function addMcpServer(agentId: number, payload: McpServerAddRequest): Promise<Agent> {
  return request<Agent>(`/agents/${agentId}/mcp-servers/`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function removeMcpServer(agentId: number, serverName: string): Promise<void> {
  await request<void>(`/agents/${agentId}/mcp-servers/${encodeURIComponent(serverName)}`, {
    method: "DELETE",
  });
}

export async function testMcpServer(agentId: number, payload: McpServerAddRequest): Promise<McpTestConnectionResponse> {
  return request<McpTestConnectionResponse>(`/agents/${agentId}/mcp-servers/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// Re-export for backwards compatibility
export { buildUrl };
