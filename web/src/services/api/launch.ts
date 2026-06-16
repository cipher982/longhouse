import { request } from "./base";

export type MachineDirectoryEntry = {
  device_id: string;
  machine_name: string;
  online: boolean;
  control_channel_status: "connected" | "disconnected";
  supports: string[];
  control_operations_by_provider: Record<string, string[]>;
  can_launch_codex: boolean;
  launchable_providers: string[];
  launch_blocked_by:
    | null
    | "control_down"
    | "no_codex_support"
    | "no_launch_support"
    | "engine_too_old"
    | "auth_failed"
    | "runtime_unreachable";
  last_seen_at: string | null;
  engine_build: string | null;
};

export type MachineDirectoryResponse = {
  machines: MachineDirectoryEntry[];
};

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

export type RemoteSessionLaunchRequest = {
  device_id: string;
  provider: string;
  cwd: string;
  git_repo?: string | null;
  git_branch?: string | null;
  project?: string | null;
  display_name?: string | null;
  initial_prompt?: string | null;
  execution_lifetime?: ExecutionLifetime | null;
  client_request_id?: string | null;
};

export type RemoteSessionLaunchResponse = {
  session_id: string;
  launch_state: LaunchState;
  execution_lifetime: ExecutionLifetime;
  launch_error_code: RemoteLaunchErrorCode | null;
  launch_error_message: string | null;
};

export type RemoteSessionContinueRequest = {
  device_id?: string | null;
  cwd?: string | null;
  client_request_id: string;
};

export async function launchRemoteSession(
  body: RemoteSessionLaunchRequest,
): Promise<RemoteSessionLaunchResponse> {
  return request<RemoteSessionLaunchResponse>("/sessions/launch", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function continueRemoteSession(
  sessionId: string,
  body: RemoteSessionContinueRequest,
): Promise<RemoteSessionLaunchResponse> {
  return request<RemoteSessionLaunchResponse>(`/sessions/${sessionId}/continue`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
