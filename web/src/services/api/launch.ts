import { request } from "./base";

export type MachineDirectoryEntry = {
  device_id: string;
  machine_name: string;
  online: boolean;
  control_channel_status: "connected" | "disconnected";
  supports: string[];
  can_launch_codex: boolean;
  launch_blocked_by:
    | null
    | "control_down"
    | "no_codex_support"
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

export type LaunchState =
  | "launching"
  | "live"
  | "launching_unknown"
  | "launch_failed"
  | "launch_orphaned";

export type RemoteLaunchErrorCode =
  | "invalid_request"
  | "device_not_enrolled"
  | "provider_unsupported"
  | "cwd_not_allowed"
  | "cwd_not_found"
  | "machine_offline"
  | "provider_launch_failed"
  | "launch_timeout";

export type RemoteSessionLaunchRequest = {
  device_id: string;
  provider: string;
  cwd: string;
  git_repo?: string | null;
  git_branch?: string | null;
  project?: string | null;
  display_name?: string | null;
  client_request_id?: string | null;
};

export type RemoteSessionLaunchResponse = {
  session_id: string;
  launch_state: LaunchState;
  launch_error_code: RemoteLaunchErrorCode | null;
  launch_error_message: string | null;
};

export type RemoteSessionContinueRequest = {
  device_id?: string | null;
  cwd?: string | null;
  client_request_id?: string | null;
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
  body: RemoteSessionContinueRequest = {},
): Promise<RemoteSessionLaunchResponse> {
  return request<RemoteSessionLaunchResponse>(`/sessions/${sessionId}/continue`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
