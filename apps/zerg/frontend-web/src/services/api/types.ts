import type { components } from "../../generated/openapi-types";

type Schemas = components["schemas"];

export type Fiche = Schemas["Fiche"];
export type FicheSummary = Fiche;
export type Course = Schemas["CourseOut"];
export type Thread = Schemas["Thread"];
export type ThreadMessage = Schemas["ThreadMessageResponse"] & { created_at?: string };
export type ThreadUpdatePayload = Schemas["ThreadUpdate"];
export type Workflow = Schemas["Workflow"];
export type WorkflowData = Schemas["WorkflowData-Output"];
export type WorkflowDataInput = Schemas["WorkflowData-Input"];
export type WorkflowNode = Schemas["WorkflowNode"];
export type WorkflowEdge = Schemas["WorkflowEdge"];

export interface WorkflowExecution {
  id: number;
  workflow_id: number;
  phase: 'waiting' | 'running' | 'finished' | 'cancelled';
  result?: unknown;
  log?: string;
  started_at?: string;
  finished_at?: string;
  triggered_by?: string;
}

export interface ExecutionStatus {
  execution_id: number;
  phase: string;
  result?: unknown;
}

export interface ExecutionLogs {
  logs: string;
}

export interface ContainerPolicy {
  enabled: boolean;
  default_image: string | null;
  network_enabled: boolean;
  user_id: number | null;
  memory_limit: string | null;
  cpus: string | null;
  timeout_secs: number;
  seccomp_profile: string | null;
}

export interface AvailableToolsResponse {
  builtin: string[];
  mcp: Record<string, string[]>;
}

export type McpServerAddRequest = components["schemas"]["MCPServerAddRequest"];
export type McpServerResponse = components["schemas"]["MCPServerResponse"];
export type McpTestConnectionResponse = components["schemas"]["MCPTestConnectionResponse"];

type FicheCreate = Schemas["FicheCreate"];
type FicheUpdate = Schemas["FicheUpdate"];

export type FicheCreatePayload = Pick<FicheCreate, "system_instructions" | "task_instructions" | "model"> &
  Partial<Omit<FicheCreate, "system_instructions" | "task_instructions" | "model">>;

export type FicheUpdatePayload = FicheUpdate;

export interface DashboardCoursesBundle {
  ficheId: number;
  courses: Course[];
}

export interface DashboardSnapshot {
  scope: "my" | "all";
  fetchedAt: string;
  coursesLimit: number;
  fiches: FicheSummary[];
  courses: DashboardCoursesBundle[];
}

export interface ModelConfig {
  id: string;
  display_name: string;
  provider: string;
  is_default: boolean;
}

export interface UserContext {
  display_name?: string;
  role?: string;
  location?: string;
  description?: string;
  servers?: Array<{
    name: string;
    ip: string;
    purpose: string;
    platform?: string;
    notes?: string;
  }>;
  integrations?: Record<string, string>;
  custom_instructions?: string;
  tools?: {
    location?: boolean;
    whoop?: boolean;
    obsidian?: boolean;
    concierge?: boolean;
    [key: string]: boolean | undefined;
  };
}

export interface UserContextResponse {
  context: UserContext;
}

export type Runner = Schemas["RunnerResponse"];
export type EnrollTokenResponse = Schemas["EnrollTokenResponse"];
export type RunnerRegisterRequest = Schemas["RunnerRegisterRequest"];
export type RunnerRegisterResponse = Schemas["RunnerRegisterResponse"];
export type RunnerUpdate = Schemas["RunnerUpdate"];
export type RunnerListResponse = Schemas["RunnerListResponse"];

export type RotateSecretResponse = {
  runner_id: number;
  runner_secret: string;
  message: string;
};

export type KnowledgeSource = Schemas["KnowledgeSource"];
export type KnowledgeSourceCreate = Schemas["KnowledgeSourceCreate"];
export type KnowledgeSourceUpdate = Schemas["KnowledgeSourceUpdate"];
export type KnowledgeDocument = Schemas["KnowledgeDocument"];
export type KnowledgeSearchResult = Schemas["KnowledgeSearchResult"];

export interface GitHubRepo {
  full_name: string;
  owner: string;
  name: string;
  private: boolean;
  default_branch: string;
  description: string | null;
  updated_at: string;
}

export interface GitHubReposResponse {
  repositories: GitHubRepo[];
  page: number;
  per_page: number;
  has_more: boolean;
}

export interface GitHubBranch {
  name: string;
  protected: boolean;
  is_default: boolean;
}

export interface GitHubBranchesResponse {
  branches: GitHubBranch[];
}
