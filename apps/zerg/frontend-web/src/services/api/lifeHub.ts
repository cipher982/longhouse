/**
 * Life Hub API service functions
 *
 * Provides functions to fetch past AI sessions from Life Hub.
 * Used by the Session Picker modal.
 */

import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SessionSummary {
  id: string;
  project: string | null;
  provider: string;
  cwd: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  duration_minutes: number | null;
  turn_count: number;
  last_user_message: string | null;
  last_ai_message: string | null;
}

export interface SessionsListResponse {
  sessions: SessionSummary[];
  total: number;
}

export interface SessionMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

export interface SessionPreview {
  id: string;
  messages: SessionMessage[];
  total_messages: number;
}

export interface SessionFilters {
  query?: string;
  project?: string;
  provider?: string;
  days_back?: number;
  limit?: number;
}

// ---------------------------------------------------------------------------
// API Functions
// ---------------------------------------------------------------------------

/**
 * List past AI sessions from Life Hub.
 */
export async function fetchSessions(filters: SessionFilters = {}): Promise<SessionsListResponse> {
  const params = new URLSearchParams();

  if (filters.query) params.set("query", filters.query);
  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.days_back) params.set("days_back", String(filters.days_back));
  if (filters.limit) params.set("limit", String(filters.limit));

  const queryString = params.toString();
  const path = `/jarvis/life-hub/sessions${queryString ? `?${queryString}` : ""}`;

  return request<SessionsListResponse>(path, { method: "GET" });
}

/**
 * Get a preview of a session's recent messages.
 */
export async function fetchSessionPreview(sessionId: string, lastN: number = 6): Promise<SessionPreview> {
  const path = `/jarvis/life-hub/sessions/${sessionId}/preview?last_n=${lastN}`;
  return request<SessionPreview>(path, { method: "GET" });
}
