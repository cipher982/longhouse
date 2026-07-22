import { request } from "./base";
import type { components } from "../../generated/openapi-types";

export type MachineDirectoryEntry = components["schemas"]["MachineDirectoryEntry"];
export type MachineDirectoryResponse = components["schemas"]["MachineDirectoryResponse"];

export async function listMachines(): Promise<MachineDirectoryResponse> {
  return request<MachineDirectoryResponse>("/timeline/machines");
}

export type WorkspaceSuggestion = {
  path: string;
  label: string;
  git_repo: string | null;
  git_branch: string | null;
  score: number;
  last_used_at: string | null;
  session_count: number;
};

export type WorkspaceSuggestionsResponse = {
  device_id: string;
  workspaces: WorkspaceSuggestion[];
};

export async function fetchWorkspaceSuggestions(
  deviceId: string,
  opts: { limit?: number } = {},
): Promise<WorkspaceSuggestionsResponse> {
  const params = new URLSearchParams();
  if (opts.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return request<WorkspaceSuggestionsResponse>(
    `/timeline/machines/${encodeURIComponent(deviceId)}/workspaces${qs ? `?${qs}` : ""}`,
  );
}

export type LaunchState =
  | "launching"
  | "live"
  | "launching_unknown"
  | "launch_failed"
  | "launch_orphaned";

export type ExecutionLifetime = "one_shot" | "live_control";

export type RemoteLaunchErrorCode =
  | "invalid_request"
  | "device_not_enrolled"
  | "provider_unsupported"
  | "cwd_not_allowed"
  | "cwd_not_found"
  | "machine_offline"
  | "provider_launch_failed"
  | "transcript_not_found"
  | "launch_timeout";

export type ConsoleSessionCreateRequest = {
  device_id: string;
  provider: string;
  cwd: string;
  project?: string | null;
  display_name?: string | null;
  launch_surface?: "web" | "ios" | "api";
};

export type ConsoleSessionCreateResponse = {
  session_id: string;
  thread_id: string;
  created: boolean;
};

export async function createConsoleSession(
  body: ConsoleSessionCreateRequest,
): Promise<ConsoleSessionCreateResponse> {
  return request<ConsoleSessionCreateResponse>("/sessions/console", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
