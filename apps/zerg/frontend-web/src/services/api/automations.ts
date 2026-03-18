import { request } from "./base";
import type {
  Automation,
  AutomationSummary,
  AutomationCreatePayload,
  AutomationUpdatePayload,
  Run,
  AutomationOverviewSnapshot,
  ContainerPolicy,
  AvailableToolsResponse,
  McpServerAddRequest,
  McpServerResponse,
  McpTestConnectionResponse,
} from "./types";

type FetchAutomationsParams = {
  scope?: "my" | "all";
  limit?: number;
  skip?: number;
};

type AutomationRunsBundleResponse = {
  fiche_id: number;
  runs: Run[];
};

type AutomationOverviewResponse = {
  scope: "my" | "all";
  fetched_at: string;
  runs_limit: number;
  fiches: AutomationSummary[];
  runs: AutomationRunsBundleResponse[];
};

type FetchAutomationOverviewParams = {
  scope?: "my" | "all";
  runsLimit?: number;
  skip?: number;
  limit?: number;
};

type RunAutomationResponse = {
  thread_id: number;
};

type CreateAutomationOptions = {
  idempotencyKey?: string;
};

export async function fetchAutomations(params: FetchAutomationsParams = {}): Promise<AutomationSummary[]> {
  const scope = params.scope ?? "my";
  const limit = params.limit ?? 100;
  const skip = params.skip ?? 0;
  const searchParams = new URLSearchParams({
    scope,
    limit: String(limit),
    skip: String(skip),
  });

  return request<AutomationSummary[]>(`/automations?${searchParams.toString()}`);
}

export async function fetchAutomationOverview(
  params: FetchAutomationOverviewParams = {}
): Promise<AutomationOverviewSnapshot> {
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

  const response = await request<AutomationOverviewResponse>(`/automations/dashboard?${searchParams.toString()}`);
  return {
    scope: response.scope,
    fetchedAt: response.fetched_at,
    runsLimit: response.runs_limit,
    automations: response.fiches,
    runs: response.runs.map((bundle) => ({
      automationId: bundle.fiche_id,
      runs: bundle.runs,
    })),
  };
}

export async function createAutomation(
  payload: AutomationCreatePayload,
  options: CreateAutomationOptions = {}
): Promise<Automation> {
  const headers = options.idempotencyKey ? { "Idempotency-Key": options.idempotencyKey } : undefined;
  return request<Automation>(`/automations`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
}

export async function fetchAutomation(automationId: number): Promise<Automation> {
  return request<Automation>(`/automations/${automationId}`);
}

export async function updateAutomation(
  automationId: number,
  payload: AutomationUpdatePayload
): Promise<Automation> {
  return request<Automation>(`/automations/${automationId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function resetAutomation(automationId: number): Promise<Automation> {
  return updateAutomation(automationId, { status: "idle", last_error: "" });
}

export async function deleteAutomation(automationId: number): Promise<void> {
  await request<void>(`/automations/${automationId}`, {
    method: "DELETE",
  });
}

export async function runAutomation(automationId: number): Promise<RunAutomationResponse> {
  return request<RunAutomationResponse>(`/automations/${automationId}/task`, {
    method: "POST",
  });
}

export async function fetchAutomationRuns(automationId: number, limit = 20): Promise<Run[]> {
  return request<Run[]>(`/automations/${automationId}/runs?limit=${limit}`);
}

export async function fetchContainerPolicy(): Promise<ContainerPolicy> {
  return request<ContainerPolicy>(`/config/container-policy`);
}

export async function fetchAutomationAvailableTools(automationId: number): Promise<AvailableToolsResponse> {
  return request<AvailableToolsResponse>(`/automations/${automationId}/mcp-servers/available-tools`);
}

export async function fetchAutomationMcpServers(automationId: number): Promise<McpServerResponse[]> {
  return request<McpServerResponse[]>(`/automations/${automationId}/mcp-servers/`);
}

export async function addAutomationMcpServer(
  automationId: number,
  payload: McpServerAddRequest
): Promise<Automation> {
  return request<Automation>(`/automations/${automationId}/mcp-servers/`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function removeAutomationMcpServer(automationId: number, serverName: string): Promise<void> {
  await request<void>(`/automations/${automationId}/mcp-servers/${encodeURIComponent(serverName)}`, {
    method: "DELETE",
  });
}

export async function testAutomationMcpServer(
  automationId: number,
  payload: McpServerAddRequest
): Promise<McpTestConnectionResponse> {
  return request<McpTestConnectionResponse>(`/automations/${automationId}/mcp-servers/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export const fetchFiches = fetchAutomations;
export const fetchDashboardSnapshot = fetchAutomationOverview;
export const createFiche = createAutomation;
export const fetchFiche = fetchAutomation;
export const updateFiche = updateAutomation;
export const resetFiche = resetAutomation;
export const deleteFiche = deleteAutomation;
export const runFiche = runAutomation;
export const fetchFicheRuns = fetchAutomationRuns;
export const fetchAvailableTools = fetchAutomationAvailableTools;
export const fetchMcpServers = fetchAutomationMcpServers;
export const addMcpServer = addAutomationMcpServer;
export const removeMcpServer = removeAutomationMcpServer;
export const testMcpServer = testAutomationMcpServer;
