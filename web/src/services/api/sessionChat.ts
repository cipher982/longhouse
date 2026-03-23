import { request } from "./base";

export interface SessionLockInfo {
  locked: boolean;
  holder?: string | null;
  time_remaining_seconds?: number | null;
  fork_available?: boolean;
}

export async function fetchSessionLockStatus(sessionId: string): Promise<SessionLockInfo> {
  return request<SessionLockInfo>(`/sessions/${sessionId}/lock`);
}

// ---------------------------------------------------------------------------
// Managed Local Session Launch
// ---------------------------------------------------------------------------

export type ManagedLocalProvider = "claude" | "codex";

export interface ManagedLocalSessionLaunchRequest {
  runner_target: string;
  cwd: string;
  provider?: ManagedLocalProvider;
  project?: string | null;
  git_repo?: string | null;
  git_branch?: string | null;
  display_name?: string | null;
  loop_mode?: "manual" | "assist" | "autopilot";
  managed_transport?: "tmux";
}

export interface ManagedLocalSessionLaunchResponse {
  session_id: string;
  provider: string;
  provider_session_id: string;
  execution_home: string;
  managed_transport: string;
  loop_mode: string;
  source_runner_id: number;
  source_runner_name: string;
  managed_session_name: string;
  attach_command: string;
}

export async function launchManagedLocalSession(
  body: ManagedLocalSessionLaunchRequest,
): Promise<ManagedLocalSessionLaunchResponse> {
  return request<ManagedLocalSessionLaunchResponse>(`/sessions/managed-local`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
