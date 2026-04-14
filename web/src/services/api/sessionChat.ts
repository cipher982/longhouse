import { request } from "./base";

export interface SessionLockInfo {
  locked: boolean;
  holder?: string | null;
  time_remaining_seconds?: number | null;
  fork_available?: boolean;
}

export interface LiveSessionSendResponse {
  accepted: boolean;
  session_id: string;
  request_id?: string | null;
  dispatch_ms?: number | null;
  error?: string | null;
  error_code?: string | null;
}

export async function fetchSessionLockStatus(
  sessionId: string,
): Promise<SessionLockInfo> {
  return request<SessionLockInfo>(`/sessions/${sessionId}/lock`);
}

export async function sendLiveSessionMessage(
  sessionId: string,
  message: string,
): Promise<LiveSessionSendResponse> {
  return request<LiveSessionSendResponse>(`/sessions/${sessionId}/send-live`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}
