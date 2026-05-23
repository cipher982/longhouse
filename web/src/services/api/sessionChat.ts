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

export interface MultipartAttachment {
  blob: Blob;
  filename: string;
}

export async function postSessionInputMultipart(
  sessionId: string,
  body: {
    text: string;
    attachments: MultipartAttachment[];
    client_request_id?: string | null;
  },
): Promise<SessionInputResponse> {
  // Multipart route is auto-intent only in v1; the server enforces this.
  const form = new FormData();
  form.append("text", body.text);
  form.append("intent", "auto");
  if (body.client_request_id) form.append("client_request_id", body.client_request_id);
  body.attachments.forEach((a) => form.append("attachments", a.blob, a.filename));
  const totalBytes = body.attachments.reduce((sum, a) => sum + a.blob.size, 0);
  const started = performance.now();
  try {
    const result = await request<SessionInputResponse>(`/sessions/${sessionId}/inputs-multipart`, {
      method: "POST",
      body: form,
    });
    const elapsedMs = Math.round(performance.now() - started);
    console.info(
      `[image-attach] web upload count=${body.attachments.length} ` +
      `total_bytes=${totalBytes} elapsed_ms=${elapsedMs}`,
    );
    return result;
  } catch (err) {
    const elapsedMs = Math.round(performance.now() - started);
    console.warn(
      `[image-attach] web upload failed count=${body.attachments.length} ` +
      `total_bytes=${totalBytes} elapsed_ms=${elapsedMs}`,
    );
    throw err;
  }
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
