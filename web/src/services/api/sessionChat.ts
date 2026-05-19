import { request } from "./base";
import type { SessionLockInfo } from "./types";

export async function fetchSessionLockStatus(sessionId: string): Promise<SessionLockInfo> {
  return request<SessionLockInfo>(`/sessions/${sessionId}/lock`);
}

export type SessionInputIntent = "auto" | "queue" | "steer";
export type SessionInputStatus =
  | "queued"
  | "delivering"
  | "delivered"
  | "cancelled"
  | "failed";
export type SessionInputOutcome = "sent" | "queued";

export interface QueuedInputSummary {
  id: number;
  text: string;
  intent: SessionInputIntent;
  status: SessionInputStatus;
  last_error?: string | null;
  created_at?: string | null;
}

export interface SessionInputResponse {
  outcome: SessionInputOutcome;
  input_id: number;
  client_request_id?: string | null;
  intent: SessionInputIntent;
  queued: QueuedInputSummary[];
}

export interface SessionInterruptResponse {
  interrupt_dispatched: boolean;
  confirmed_stopped: boolean;
  session_id: string;
  exit_code: number | null;
  error: string | null;
  released_lock: boolean;
}

export async function postSessionInput(
  sessionId: string,
  body: { text: string; intent: SessionInputIntent; client_request_id?: string | null },
): Promise<SessionInputResponse> {
  return request<SessionInputResponse>(`/sessions/${sessionId}/input`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function fetchSessionInputs(
  sessionId: string,
): Promise<QueuedInputSummary[]> {
  return request<QueuedInputSummary[]>(`/sessions/${sessionId}/inputs`);
}

export async function cancelSessionInput(
  sessionId: string,
  inputId: number,
): Promise<{ cancelled: boolean; input_id: number }> {
  return request(`/sessions/${sessionId}/inputs/${inputId}`, {
    method: "DELETE",
  });
}

export async function interruptLiveSession(
  sessionId: string,
): Promise<SessionInterruptResponse> {
  return request<SessionInterruptResponse>(`/sessions/${sessionId}/interrupt-live`, {
    method: "POST",
  });
}
