import { request } from "./base";

export type MachineDirectoryEntry = {
  device_id: string;
  machine_name: string;
  online: boolean;
  supports: string[];
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
  launch_error_code: string | null;
  launch_error_message: string | null;
};

export async function launchRemoteSession(
  body: RemoteSessionLaunchRequest,
): Promise<RemoteSessionLaunchResponse> {
  return request<RemoteSessionLaunchResponse>("/sessions/launch", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
