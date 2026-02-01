/**
 * Agents API service functions
 *
 * Provides functions to fetch agent sessions and events from the shipper.
 * Used by the Sessions Timeline pages.
 */

import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentSession {
  id: string;
  provider: string;
  project: string | null;
  device_id: string | null;
  cwd: string | null;
  git_repo: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_calls: number;
}

export interface AgentSessionsListResponse {
  sessions: AgentSession[];
  total: number;
}

export interface AgentSessionSummary {
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

export interface AgentSessionSummaryListResponse {
  sessions: AgentSessionSummary[];
  total: number;
}

export interface AgentSessionPreviewMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

export interface AgentSessionPreview {
  id: string;
  messages: AgentSessionPreviewMessage[];
  total_messages: number;
}

export type AgentSessionStatus = "working" | "thinking" | "idle" | "completed" | "active";
export type AgentAttentionLevel = "hard" | "needs" | "soft" | "auto";

export interface AgentActiveSession {
  id: string;
  project: string | null;
  provider: string;
  cwd: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string;
  status: AgentSessionStatus;
  attention: AgentAttentionLevel;
  duration_minutes: number;
  last_user_message: string | null;
  last_assistant_message: string | null;
  message_count: number;
  tool_calls: number;
}

export interface AgentActiveSessionsResponse {
  sessions: AgentActiveSession[];
  total: number;
  last_refresh: string;
}

export interface AgentEvent {
  id: number;
  role: string;
  content_text: string | null;
  tool_name: string | null;
  tool_input_json: Record<string, unknown> | null;
  tool_output_text: string | null;
  timestamp: string;
}

export interface AgentEventsListResponse {
  events: AgentEvent[];
  total: number;
}

export interface AgentSessionFilters {
  project?: string;
  provider?: string;
  device_id?: string;
  days_back?: number;
  query?: string;
  limit?: number;
  offset?: number;
}

export interface AgentSessionSummaryFilters {
  query?: string;
  project?: string;
  provider?: string;
  device_id?: string;
  days_back?: number;
  limit?: number;
  offset?: number;
}

export interface AgentActiveSessionFilters {
  project?: string;
  attention?: AgentAttentionLevel;
  status?: AgentSessionStatus;
  limit?: number;
  days_back?: number;
}

export interface AgentFiltersResponse {
  projects: string[];
  providers: string[];
}

export interface AgentDemoSeedResponse {
  seeded: boolean;
  sessions_created: number;
}

// ---------------------------------------------------------------------------
// API Functions
// ---------------------------------------------------------------------------

/**
 * List agent sessions with optional filters.
 */
export async function fetchAgentSessions(
  filters: AgentSessionFilters = {}
): Promise<AgentSessionsListResponse> {
  const params = new URLSearchParams();

  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.device_id) params.set("device_id", filters.device_id);
  if (filters.days_back) params.set("days_back", String(filters.days_back));
  if (filters.query) params.set("query", filters.query);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));

  const queryString = params.toString();
  const path = `/agents/sessions${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionsListResponse>(path, { method: "GET" });
}

/**
 * List agent session summaries for picker UI.
 */
export async function fetchAgentSessionSummaries(
  filters: AgentSessionSummaryFilters = {}
): Promise<AgentSessionSummaryListResponse> {
  const params = new URLSearchParams();

  if (filters.query) params.set("query", filters.query);
  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.device_id) params.set("device_id", filters.device_id);
  if (filters.days_back) params.set("days_back", String(filters.days_back));
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));

  const queryString = params.toString();
  const path = `/agents/sessions/summary${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionSummaryListResponse>(path, { method: "GET" });
}

/**
 * Get a preview of a session's recent messages.
 */
export async function fetchAgentSessionPreview(
  sessionId: string,
  lastN: number = 6
): Promise<AgentSessionPreview> {
  const path = `/agents/sessions/${sessionId}/preview?last_n=${lastN}`;
  return request<AgentSessionPreview>(path, { method: "GET" });
}

/**
 * List sessions for Forum live mode.
 */
export async function fetchAgentActiveSessions(
  filters: AgentActiveSessionFilters = {}
): Promise<AgentActiveSessionsResponse> {
  const params = new URLSearchParams();

  if (filters.project) params.set("project", filters.project);
  if (filters.attention) params.set("attention", filters.attention);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.days_back) params.set("days_back", String(filters.days_back));

  const queryString = params.toString();
  const path = `/agents/sessions/active${queryString ? `?${queryString}` : ""}`;

  return request<AgentActiveSessionsResponse>(path, { method: "GET" });
}

/**
 * Get a single session by ID.
 */
export async function fetchAgentSession(sessionId: string): Promise<AgentSession> {
  return request<AgentSession>(`/agents/sessions/${sessionId}`, { method: "GET" });
}

/**
 * Get events for a session.
 */
export async function fetchAgentSessionEvents(
  sessionId: string,
  options: { roles?: string; limit?: number; offset?: number } = {}
): Promise<AgentEventsListResponse> {
  const params = new URLSearchParams();

  if (options.roles) params.set("roles", options.roles);
  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));

  const queryString = params.toString();
  const path = `/agents/sessions/${sessionId}/events${queryString ? `?${queryString}` : ""}`;

  return request<AgentEventsListResponse>(path, { method: "GET" });
}

/**
 * Get distinct filter values for dropdowns.
 */
export async function fetchAgentFilters(
  daysBack: number = 90
): Promise<AgentFiltersResponse> {
  return request<AgentFiltersResponse>(`/agents/filters?days_back=${daysBack}`, {
    method: "GET",
  });
}

/**
 * Seed demo sessions for the timeline.
 */
export async function seedAgentDemoSessions(): Promise<AgentDemoSeedResponse> {
  return request<AgentDemoSeedResponse>("/agents/demo", { method: "POST" });
}
