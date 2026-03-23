import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EmailKeyStatus {
  key: string;
  configured: boolean;
  source: string | null; // "db" | "env" | null
}

export interface EmailStatus {
  configured: boolean;
  source: string | null;
  keys: EmailKeyStatus[];
}

export interface EmailConfigPayload {
  aws_ses_access_key_id?: string;
  aws_ses_secret_access_key?: string;
  aws_ses_region?: string;
  from_email?: string;
  notify_email?: string;
  digest_email?: string;
  alert_email?: string;
}

export interface EmailTestResult {
  success: boolean;
  message: string;
  message_id?: string;
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export async function fetchEmailStatus(): Promise<EmailStatus> {
  return request<EmailStatus>("/system/email/status");
}

export async function saveEmailConfig(
  data: EmailConfigPayload
): Promise<{ success: boolean; keys_saved: number }> {
  return request("/system/email/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function testEmail(
  to_email?: string
): Promise<EmailTestResult> {
  return request<EmailTestResult>("/system/email/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ to_email: to_email || null }),
  });
}

export async function deleteEmailConfig(): Promise<{ success: boolean; keys_deleted: number }> {
  return request("/system/email/config", { method: "DELETE" });
}
