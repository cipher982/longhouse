import type { components } from "../../generated/openapi-types";

type Schemas = components["schemas"];

export type SessionLockInfo = Schemas["SessionLockInfo"];

export type TokenBreakdown = Schemas["TokenBreakdown"];
export type UsageLimit = Schemas["UsageLimit"];
export type UserUsageResponse = Schemas["UserUsageResponse"];
export type PeriodUsage = Schemas["PeriodUsage"];
export type UserUsageSummary = Schemas["UserUsageSummary"];
export type AdminUserRow = Schemas["AdminUserRow"];
export type AdminUsersResponse = Schemas["AdminUsersResponse"];
export type DailyBreakdown = Schemas["DailyBreakdown"];
export type TopAutomationUsage = Schemas["TopAutomationUsage"];
export type AdminUserDetailResponse = Schemas["AdminUserDetailResponse"];
export type OpsSummary = Schemas["OpsSummary"];
export type OpsTopAutomation = Schemas["OpsTopAutomation"];
export type MachineHealthItemResponse = Schemas["MachineHealthItemResponse"];
export type MachineHealthListResponse = Schemas["MachineHealthListResponse"];
export type MachineHealthStatusCountsResponse = Schemas["MachineHealthStatusCountsResponse"];
export type ManagedTurnProviderSummaryResponse = Schemas["ManagedTurnProviderSummaryResponse"];
export type ManagedTurnSummaryResponse = Schemas["ManagedTurnSummaryResponse"];
export type ManagedTurnsSummaryEnvelopeResponse = Schemas["ManagedTurnsSummaryEnvelopeResponse"];
export type ObservabilityOverviewResponse = Schemas["ObservabilityOverviewResponse"];
export type ProductHealthCheckListResponse = Schemas["ProductHealthCheckListResponse"];
export type ProductHealthCheckLivePreviewResponse = Schemas["ProductHealthCheckLivePreviewResponse"];
export type ProductHealthCheckSummaryResponse = Schemas["ProductHealthCheckSummaryResponse"];
export type SlowTurnItemResponse = Schemas["SlowTurnItemResponse"];
export type SlowTurnsListResponse = Schemas["SlowTurnsListResponse"];

export type EmailKeyStatus = Schemas["EmailKeyStatus"];
export type EmailStatusResponse = Schemas["EmailStatusResponse"];

export interface GmailWatchState {
  status: "active" | "failed" | "not_configured";
  method?: "pubsub" | "legacy" | null;
  history_id?: number | null;
  watch_expiry?: number | null;
  error?: string | null;
}

export interface GmailConnectResponse {
  status: "connected";
  connector_id: number;
  mailbox_email: string | null;
  watch: GmailWatchState;
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

export interface ModelConfig {
  id: string;
  display_name: string;
  provider: string;
  is_default: boolean;
}

export type Runner = Schemas["RunnerResponse"];
export type EnrollTokenResponse = Schemas["EnrollTokenResponse"];
export type RunnerRegisterRequest = Schemas["RunnerRegisterRequest"];
export type RunnerRegisterResponse = Schemas["RunnerRegisterResponse"];
export type RunnerUpdate = Schemas["RunnerUpdate"];
export type RunnerListResponse = Schemas["RunnerListResponse"];
export type RunnerDoctorCheck = Schemas["RunnerDoctorCheck"];
export type RunnerDoctorResponse = Schemas["RunnerDoctorResponse"];
export type RunnerJob = Schemas["RunnerJobResponse"];
export type RunnerJobListResponse = Schemas["RunnerJobListResponse"];

export type RotateSecretResponse = {
  runner_id: number;
  runner_secret: string;
  message: string;
};

export type RunnerStatusItem = Schemas["RunnerStatusItem"];
export type RunnerStatusResponse = Schemas["RunnerStatusResponse"];

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
