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

export interface AgentFiltersResponse {
  projects: string[];
  providers: string[];
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
