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
  intent: SessionInputIntent;
  queued: QueuedInputSummary[];
}

export async function postSessionInput(
  sessionId: string,
  body: { text: string; intent: SessionInputIntent },
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
