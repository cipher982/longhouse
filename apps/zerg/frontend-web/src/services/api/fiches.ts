import { request, buildUrl } from "./base";
import type {
  Fiche,
  FicheSummary,
  FicheCreatePayload,
  FicheUpdatePayload,
  Run,
  DashboardSnapshot,
  ContainerPolicy,
  AvailableToolsResponse,
  McpServerAddRequest,
  McpServerResponse,
  McpTestConnectionResponse,
} from "./types";

type FetchFichesParams = {
  scope?: "my" | "all";
  limit?: number;
  skip?: number;
};

type DashboardRunsBundleResponse = {
  fiche_id: number;
  runs: Run[];
};

type DashboardSnapshotResponse = {
  scope: "my" | "all";
  fetched_at: string;
  runs_limit: number;
  fiches: FicheSummary[];
  runs: DashboardRunsBundleResponse[];
};

type FetchDashboardParams = {
  scope?: "my" | "all";
  runsLimit?: number;
  skip?: number;
  limit?: number;
};

type RunFicheResponse = {
  thread_id: number;
};

export async function fetchFiches(params: FetchFichesParams = {}): Promise<FicheSummary[]> {
  const scope = params.scope ?? "my";
  const limit = params.limit ?? 100;
  const skip = params.skip ?? 0;
  const searchParams = new URLSearchParams({
    scope,
    limit: String(limit),
    skip: String(skip),
  });

  return request<FicheSummary[]>(`/fiches?${searchParams.toString()}`);
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

  const response = await request<DashboardSnapshotResponse>(`/fiches/dashboard?${searchParams.toString()}`);
  return {
    scope: response.scope,
    fetchedAt: response.fetched_at,
    runsLimit: response.runs_limit,
    fiches: response.fiches,
    runs: response.runs.map((bundle) => ({
      ficheId: bundle.fiche_id,
      runs: bundle.runs,
    })),
  };
}

export async function createFiche(payload: FicheCreatePayload): Promise<Fiche> {
  return request<Fiche>(`/fiches`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchFiche(ficheId: number): Promise<Fiche> {
  return request<Fiche>(`/fiches/${ficheId}`);
}

export async function updateFiche(ficheId: number, payload: FicheUpdatePayload): Promise<Fiche> {
  return request<Fiche>(`/fiches/${ficheId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function resetFiche(ficheId: number): Promise<Fiche> {
  return updateFiche(ficheId, { status: "idle", last_error: "" });
}

export async function runFiche(ficheId: number): Promise<RunFicheResponse> {
  return request<RunFicheResponse>(`/fiches/${ficheId}/task`, {
    method: "POST",
  });
}

export async function fetchFicheRuns(ficheId: number, limit = 20): Promise<Run[]> {
  return request<Run[]>(`/fiches/${ficheId}/runs?limit=${limit}`);
}

export async function fetchContainerPolicy(): Promise<ContainerPolicy> {
  return request<ContainerPolicy>(`/config/container-policy`);
}

export async function fetchAvailableTools(ficheId: number): Promise<AvailableToolsResponse> {
  return request<AvailableToolsResponse>(`/fiches/${ficheId}/mcp-servers/available-tools`);
}

export async function fetchMcpServers(ficheId: number): Promise<McpServerResponse[]> {
  return request<McpServerResponse[]>(`/fiches/${ficheId}/mcp-servers/`);
}

export async function addMcpServer(ficheId: number, payload: McpServerAddRequest): Promise<Fiche> {
  return request<Fiche>(`/fiches/${ficheId}/mcp-servers/`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function removeMcpServer(ficheId: number, serverName: string): Promise<void> {
  await request<void>(`/fiches/${ficheId}/mcp-servers/${encodeURIComponent(serverName)}`, {
    method: "DELETE",
  });
}

export async function testMcpServer(
  ficheId: number,
  payload: McpServerAddRequest
): Promise<McpTestConnectionResponse> {
  return request<McpTestConnectionResponse>(`/fiches/${ficheId}/mcp-servers/test`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export { buildUrl };
