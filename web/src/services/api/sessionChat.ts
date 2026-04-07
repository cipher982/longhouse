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
