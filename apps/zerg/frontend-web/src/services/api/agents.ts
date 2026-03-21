/**
 * Browser timeline/session archive API service functions.
 *
 * These routes back the cookie-authenticated browser session archive UI.
 * Device-token ingest and machine workflows stay on `/api/agents/*`.
 */

import { request } from "./base";

const TIMELINE_API_PREFIX = "/timeline";
const TIMELINE_SESSIONS_PREFIX = `${TIMELINE_API_PREFIX}/sessions`;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentSession {
  id: string;
  provider: string;
  project: string | null;
  device_id: string | null;
  environment: string | null;
  cwd: string | null;
  git_repo: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string | null;
  timeline_anchor_at?: string | null;
  status?: AgentSessionStatus | null;
  presence_state?: PresenceState | null;
  presence_tool?: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_calls: number;
  summary: string | null;
  summary_title: string | null;
  first_user_message: string | null;
  match_event_id?: number | null;
  match_snippet?: string | null;
  match_role?: string | null;
  match_score?: number | null;
  thread_root_session_id: string;
  thread_head_session_id: string;
  thread_continuation_count: number;
  continued_from_session_id: string | null;
  continuation_kind: string | null;
  origin_label: string | null;
  branched_from_event_id: number | null;
  is_writable_head: boolean;
  loop_mode: SessionLoopMode;
}

export interface AgentSessionsListResponse {
  sessions: AgentSession[];
  total: number;
  has_real_sessions: boolean;
}

export interface AgentSessionThreadResponse {
  root_session_id: string;
  head_session_id: string;
  sessions: AgentSession[];
}

export interface AgentSessionProjectionItem {
  kind: "event" | "seam";
  session_id: string;
  timestamp: string;
  event?: AgentEvent | null;
  continued_from_session_id?: string | null;
  continuation_kind?: string | null;
  origin_label?: string | null;
  parent_origin_label?: string | null;
  parent_continuation_kind?: string | null;
  branched_from_event_id?: number | null;
}

export interface AgentSessionProjectionResponse {
  root_session_id: string;
  focus_session_id: string;
  head_session_id: string;
  path_session_ids: string[];
  items: AgentSessionProjectionItem[];
  total: number;
  branch_mode?: "head" | "all";
  abandoned_events?: number;
}

export interface AgentSessionSummary {
  id: string;
  project: string | null;
  provider: string;
  cwd: string | null;
  git_repo: string | null;
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

export type AgentSessionStatus =
  | "working"
  | "thinking"
  | "idle"
  | "completed"
  | "active";
export type AgentAttentionLevel = "hard" | "needs" | "soft" | "auto";

export type PresenceState =
  | "thinking"
  | "running"
  | "idle"
  | "needs_user"
  | "blocked"
  | (string & {});

export interface AgentActiveSession {
  id: string;
  project: string | null;
  provider: string;
  cwd: string | null;
  git_repo: string | null;
  git_branch: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string;
  timeline_anchor_at?: string;
  status: AgentSessionStatus;
  attention: AgentAttentionLevel;
  duration_minutes: number;
  last_user_message: string | null;
  last_assistant_message: string | null;
  message_count: number;
  tool_calls: number;
  // Real-time presence (null when no hook signal received yet)
  presence_state: PresenceState | null;
  presence_tool: string | null;
  presence_updated_at: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  // User-driven bucket
  user_state: "active" | "parked" | "snoozed" | "archived";
  loop_mode: SessionLoopMode;
}

export type UserStateAction = "park" | "snooze" | "archive" | "resume";
export type SessionLoopMode = "manual" | "assist" | "autopilot";

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
  tool_call_id: string | null;
  timestamp: string;
  in_active_context?: boolean;
  branch_id?: number | null;
  is_head_branch?: boolean;
}

export interface AgentEventsListResponse {
  events: AgentEvent[];
  total: number;
  branch_mode?: "head" | "all";
  abandoned_events?: number;
}

export interface AgentSessionFilters {
  project?: string;
  provider?: string;
  environment?: string;
  device_id?: string;
  days_back?: number;
  query?: string;
  limit?: number;
  offset?: number;
  mode?: "lexical" | "semantic" | "hybrid";
  sort?: "relevance" | "recency";
  hide_autonomous?: boolean;
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
  machines: string[];
}

// ---------------------------------------------------------------------------
// API Functions
// ---------------------------------------------------------------------------

/**
 * List agent sessions with optional filters.
 */
export async function fetchAgentSessions(
  filters: AgentSessionFilters = {},
): Promise<AgentSessionsListResponse> {
  const params = new URLSearchParams();

  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.environment) params.set("environment", filters.environment);
  if (filters.device_id) params.set("device_id", filters.device_id);
  if (filters.days_back) params.set("days_back", String(filters.days_back));
  if (filters.query) params.set("query", filters.query);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  if (filters.mode && filters.mode !== "lexical")
    params.set("mode", filters.mode);
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.hide_autonomous === false) params.set("hide_autonomous", "false");

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionsListResponse>(path, { method: "GET" });
}

/**
 * List agent session summaries for picker UI.
 */
export async function fetchAgentSessionSummaries(
  filters: AgentSessionSummaryFilters = {},
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
  const path = `${TIMELINE_SESSIONS_PREFIX}/summary${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionSummaryListResponse>(path, { method: "GET" });
}

/**
 * Get a preview of a session's recent messages.
 */
export async function fetchAgentSessionPreview(
  sessionId: string,
  lastN: number = 6,
): Promise<AgentSessionPreview> {
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/preview?last_n=${lastN}`;
  return request<AgentSessionPreview>(path, { method: "GET" });
}

/**
 * List sessions for the live sessions view.
 */
export async function fetchAgentActiveSessions(
  filters: AgentActiveSessionFilters = {},
): Promise<AgentActiveSessionsResponse> {
  const params = new URLSearchParams();

  if (filters.project) params.set("project", filters.project);
  if (filters.attention) params.set("attention", filters.attention);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.days_back) params.set("days_back", String(filters.days_back));

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/active${queryString ? `?${queryString}` : ""}`;

  return request<AgentActiveSessionsResponse>(path, { method: "GET" });
}

/**
 * Get a single session by ID.
 */
export async function fetchAgentSession(
  sessionId: string,
): Promise<AgentSession> {
  return request<AgentSession>(`${TIMELINE_SESSIONS_PREFIX}/${sessionId}`, {
    method: "GET",
  });
}

export async function fetchAgentSessionThread(
  sessionId: string,
): Promise<AgentSessionThreadResponse> {
  return request<AgentSessionThreadResponse>(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/thread`,
    { method: "GET" },
  );
}

export async function fetchAgentSessionProjection(
  sessionId: string,
  options: {
    limit?: number;
    offset?: number;
    branch_mode?: "head" | "all";
  } = {},
): Promise<AgentSessionProjectionResponse> {
  const params = new URLSearchParams();

  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  if (options.branch_mode) params.set("branch_mode", options.branch_mode);

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/projection${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionProjectionResponse>(path, { method: "GET" });
}

/**
 * Get events for a session.
 */
export async function fetchAgentSessionEvents(
  sessionId: string,
  options: {
    roles?: string;
    limit?: number;
    offset?: number;
    branch_mode?: "head" | "all";
  } = {},
): Promise<AgentEventsListResponse> {
  const params = new URLSearchParams();

  if (options.roles) params.set("roles", options.roles);
  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  if (options.branch_mode) params.set("branch_mode", options.branch_mode);

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/events${queryString ? `?${queryString}` : ""}`;

  return request<AgentEventsListResponse>(path, { method: "GET" });
}

/**
 * Get distinct filter values for dropdowns.
 */
export async function fetchAgentFilters(
  daysBack: number = 90,
): Promise<AgentFiltersResponse> {
  return request<AgentFiltersResponse>(
    `${TIMELINE_API_PREFIX}/filters?days_back=${daysBack}`,
    {
      method: "GET",
    },
  );
}

// ---------------------------------------------------------------------------
// Semantic Search & Recall Types
// ---------------------------------------------------------------------------

export interface SemanticSearchFilters {
  query: string;
  project?: string;
  provider?: string;
  environment?: string;
  days_back?: number;
  limit?: number;
}

export interface SemanticSearchResponse {
  sessions: AgentSession[];
  total: number;
  has_real_sessions: boolean;
}

export interface RecallMatch {
  session_id: string;
  chunk_index: number;
  score: number;
  event_index_start: number | null;
  event_index_end: number | null;
  total_events: number;
  context: RecallContextTurn[];
  match_event_id: number | null;
}

export interface RecallContextTurn {
  index: number;
  role: string;
  content: string;
  tool_name: string | null;
  is_match: boolean;
}

export interface RecallResponse {
  matches: RecallMatch[];
  total: number;
}

export interface RecallFilters {
  query: string;
  project?: string;
  since_days?: number;
  max_results?: number;
  context_turns?: number;
}

// ---------------------------------------------------------------------------
// Semantic Search & Recall API Functions
// ---------------------------------------------------------------------------

/**
 * Semantic search for sessions using embeddings.
 */
export async function fetchSemanticSearch(
  filters: SemanticSearchFilters,
): Promise<SemanticSearchResponse> {
  const params = new URLSearchParams();
  params.set("query", filters.query);
  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.environment) params.set("environment", filters.environment);
  if (filters.days_back) params.set("days_back", String(filters.days_back));
  if (filters.limit) params.set("limit", String(filters.limit));

  return request<SemanticSearchResponse>(
    `${TIMELINE_SESSIONS_PREFIX}/semantic?${params.toString()}`,
    { method: "GET" },
  );
}

/**
 * Recall: turn-level semantic search with context windows.
 */
export async function fetchRecall(
  filters: RecallFilters,
): Promise<RecallResponse> {
  const params = new URLSearchParams();
  params.set("query", filters.query);
  if (filters.project) params.set("project", filters.project);
  if (filters.since_days) params.set("since_days", String(filters.since_days));
  if (filters.max_results)
    params.set("max_results", String(filters.max_results));
  if (filters.context_turns)
    params.set("context_turns", String(filters.context_turns));

  return request<RecallResponse>(
    `${TIMELINE_API_PREFIX}/recall?${params.toString()}`,
    { method: "GET" },
  );
}

export interface DemoSeedResponse {
  seeded: boolean;
  sessions_created: number;
  sessions_failed: number;
  sessions_deleted: number;
}

/**
 * Seed demo sessions for the timeline (idempotent).
 */
export async function seedDemoSessions(options?: {
  replace?: boolean;
}): Promise<DemoSeedResponse> {
  const params = new URLSearchParams();
  if (options?.replace) params.set("replace", "true");
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  return request<DemoSeedResponse>(`${TIMELINE_API_PREFIX}/demo${suffix}`, {
    method: "POST",
  });
}

/**
 * Set user-driven bucket state for a session (park/snooze/archive/resume).
 */
// ---------------------------------------------------------------------------
// Briefing
// ---------------------------------------------------------------------------

export interface BriefingResponse {
  project: string;
  session_count: number;
  briefing: string | null;
}

/**
 * Fetch a project briefing — recent session summaries + curated insights.
 * Requires embeddings to be configured; returns null briefing if unavailable.
 */
export async function fetchAgentBriefing(
  project: string,
  limit: number = 5,
): Promise<BriefingResponse> {
  const params = new URLSearchParams({ project, limit: String(limit) });
  return request<BriefingResponse>(`${TIMELINE_API_PREFIX}/briefing?${params}`);
}

// ---------------------------------------------------------------------------
// Session actions
// ---------------------------------------------------------------------------

export async function setSessionAction(
  sessionId: string,
  action: UserStateAction,
): Promise<{ session_id: string; user_state: string }> {
  return request(`${TIMELINE_SESSIONS_PREFIX}/${sessionId}/action`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export async function setSessionLoopMode(
  sessionId: string,
  loopMode: SessionLoopMode,
): Promise<{ session_id: string; loop_mode: SessionLoopMode }> {
  return request(`${TIMELINE_SESSIONS_PREFIX}/${sessionId}/loop-mode`, {
    method: "PATCH",
    body: JSON.stringify({ loop_mode: loopMode }),
  });
}
