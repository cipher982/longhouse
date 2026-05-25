/**
 * Browser timeline/session archive API service functions.
 *
 * These routes back the cookie-authenticated browser session archive UI.
 * Device-token ingest and machine workflows stay on `/api/agents/*`.
 */

import { buildUrl, request } from "./base";

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
  runtime_phase?: string | null;
  phase_started_at?: string | null;
  last_progress_at?: string | null;
  runtime_source?: string | null;
  terminal_state?: string | null;
  runtime_version?: number | null;
  status?: AgentSessionStatus | null;
  presence_state?: PresenceState | null;
  presence_tool?: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  runtime_display?: SessionRuntimeDisplay | null;
  runtime_facts?: SessionLivenessFacts | null;
  timeline_card?: TimelineCardPresentation | null;
  transcript_preview?: SessionTranscriptPreview | null;
  user_messages: number;
  assistant_messages: number;
  tool_calls: number;
  summary: string | null;
  summary_title: string | null;
  summary_status?: "ready" | "pending" | "failed" | "unavailable" | (string & {}) | null;
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
  home_label: string | null;
  branched_from_event_id: number | null;
  is_writable_head: boolean;
  control?: SessionControl | null;
  capabilities?: SessionCapabilities | null;
  loop_mode: SessionLoopMode;
  user_state?: string;
  /** Remote-launch lifecycle state; null for sessions created before remote-launch. */
  launch_state?: "launching" | "live" | "launching_unknown" | "launch_failed" | "launch_orphaned" | null;
  launch_error_code?: string | null;
  launch_error_message?: string | null;
}

export interface SessionTranscriptPreview {
  event_id: number;
  text: string;
  event_origin: string;
  timestamp: string;
  is_provisional: boolean;
  is_complete: boolean;
  content_cursor?: string | null;
  is_stale: boolean;
  stale_reason?: "freshness_window_expired" | "missing_preview_timestamp" | "superseded_by_durable" | null;
}

export interface SessionRuntimeDisplay {
  truth_tier: "none" | "stale" | "fresh" | "managed-local" | (string & {});
  signal_tier?: "phase_signal" | "process_binding" | "transcript_progress" | "none" | (string & {});
  state: PresenceState | null;
  tone:
    | "inactive"
    | "thinking"
    | "running"
    | "blocked"
    | "stalled"
    | "idle"
    | "active"
    | (string & {});
  headline: string;
  detail: string | null;
  phase_label: string;
  compact_tool_label: string | null;
  is_live: boolean;
  is_executing: boolean;
  needs_attention: boolean;
  is_idle: boolean;
  is_stalled?: boolean;
  is_managed_local_truth: boolean;
  has_signal: boolean;
  control_path?: "managed" | "unmanaged" | (string & {});
  activity_recency?: "live" | "recent" | "stale" | "none" | (string & {});
  lifecycle?: "open" | "closed" | "unknown" | (string & {});
  host_state?: "online" | "stale" | "offline" | "unknown" | (string & {});
  terminal_reason?:
    | "provider_signal"
    | "process_gone"
    | "host_expired"
    | "user_closed"
    | "host_reported"
    | "bridge_stop"
    | "terminal_disconnected"
    | null
    | (string & {});
}

export interface HostObservation {
  state: "online" | "stale" | "offline" | "unknown" | (string & {});
  last_seen_at?: string | null;
  source?: string | null;
}

export interface ProcessObservation {
  status: "observed" | "not_observed" | "unknown" | (string & {});
  pid?: number | null;
  process_start_time?: string | null;
  observed_at?: string | null;
  last_seen_at?: string | null;
  source_mtime?: string | null;
  source_path?: string | null;
  reason?: string | null;
  source?: string | null;
}

export interface PhaseObservation {
  kind?: string | null;
  tool?: string | null;
  source?: string | null;
  observed_at?: string | null;
  expires_at?: string | null;
}

export interface ActivityObservation {
  last_transcript_at?: string | null;
  last_runtime_signal_at?: string | null;
  last_progress_at?: string | null;
}

export interface ControlObservation {
  state?: "online" | "degraded" | "offline" | "unknown" | "none" | (string & {});
  reason?: string | null;
  source?: string | null;
  last_seen_at?: string | null;
  expires_at?: string | null;
  transport?: string | null;
}

export interface LifecycleFact {
  state: "open" | "closed" | "unknown" | (string & {});
  reason?: string | null;
  observed_at?: string | null;
}

export interface SessionLivenessFacts {
  control_path: "managed" | "unmanaged" | (string & {});
  control?: ControlObservation;
  process_state: "running" | "closed" | "unknown" | (string & {});
  host: HostObservation;
  process: ProcessObservation;
  phase: PhaseObservation;
  activity: ActivityObservation;
  lifecycle: LifecycleFact;
}

export interface TimelineBadgePresentation {
  label: string;
  tone: "neutral" | "inactive" | "active" | "thinking" | "running" | "blocked" | "stalled" | "idle" | "closed" | (string & {});
}

export interface TimelineStatusPresentation extends TimelineBadgePresentation {
  seen_at: string | null;
  seen_at_prefix: string;
}

export interface TimelineCardPresentation {
  ownership: TimelineBadgePresentation;
  status: TimelineStatusPresentation;
  border_tone: "inactive" | "active" | "thinking" | "running" | "blocked" | "stalled" | "idle" | "closed" | (string & {});
}

export interface SessionControl {
  source_runner_id: number | null;
  source_runner_name: string | null;
  attach_command?: string | null;
}

export type SendDisabledReason = "session_closed" | "control_offline" | "input_not_supported" | "read_only";

export interface SessionCapabilities {
  live_control_available: boolean;
  host_reattach_available: boolean;
  reply_to_live_session_available: boolean;
  can_queue_next_input?: boolean;
  can_steer_active_turn?: boolean;
  display_label?: string;
  display_detail?: string;
  display_tone?: "success" | "warning" | "neutral" | (string & {});
  input_mode?: "live" | "offline" | "read_only" | (string & {});
  default_input_intent?: "auto" | "steer" | "queue" | "none" | (string & {});
  composer_enabled?: boolean;
  composer_placeholder?: string;
  composer_disabled_reason?: string | null;
  send_disabled_reason?: SendDisabledReason | null;
  /**
   * True when this session accepts image attachments on input. Today this is
   * codex_app_server + live_control_available; the server is the source of
   * truth so the web client doesn't have to know the transport set.
   */
  attach_images?: boolean;
}

export interface AgentSessionsListResponse {
  sessions: AgentSession[];
  total: number;
  has_real_sessions: boolean;
}

export interface TimelineSessionCard {
  thread_id: string;
  timeline_anchor_at: string | null;
  head: AgentSession;
  detail: AgentSession;
  root: AgentSession;
  continuation_count: number;
  started_origin_label: string | null;
  head_origin_label: string | null;
}

export interface TimelineSessionsListResponse {
  sessions: TimelineSessionCard[];
  total: number;
  has_real_sessions: boolean;
  query_grouping_mode?: "grouped_results";
  query_grouping_has_more?: boolean;
  query_grouping_source_count?: number;
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
  page_offset?: number;
  branch_mode?: "head" | "all";
  abandoned_events?: number;
}

export interface AgentSessionWorkspaceResponse {
  session: AgentSession;
  thread: AgentSessionThreadResponse;
  projection: AgentSessionProjectionResponse;
}

export type AgentSessionTurnState =
  | "created"
  | "send_accepted"
  | "active"
  | "terminal"
  | "durable"
  | "failed";

export interface AgentSessionTurn {
  id: number;
  session_id: string;
  request_id: string | null;
  state: AgentSessionTurnState | (string & {});
  terminal_phase: string | null;
  error_code: string | null;
  user_event_id: number | null;
  durable_assistant_event_id: number | null;
  baseline_event_id: number | null;
  baseline_observation_cursor: number | null;
  user_submitted_at: string;
  send_accepted_at: string | null;
  active_phase_observed_at: string | null;
  terminal_at: string | null;
  durable_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AgentSessionTurnsListResponse {
  turns: AgentSessionTurn[];
  total: number;
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

export type PresenceState =
  | "thinking"
  | "running"
  | "idle"
  | "needs_user"
  | "blocked"
  | "stalled"
  | (string & {});

export type UserStateAction = "park" | "snooze" | "archive" | "resume";
export type SessionLoopMode = "assist" | "autopilot";
export interface AgentEventInputOrigin {
  authored_via: "longhouse" | "terminal";
  session_input_id?: number | null;
  client_request_id?: string | null;
}

export interface AgentEvent {
  id: number;
  role: string;
  content_text: string | null;
  raw_content_text?: string | null;
  input_origin?: AgentEventInputOrigin | null;
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

export interface AgentFiltersResponse {
  projects: string[];
  providers: string[];
  machines: string[];
}

export interface TimelineSessionUpsertEvent {
  session: TimelineSessionCard;
  total?: number;
  has_real_sessions?: boolean;
}

export interface TimelineSessionRemoveEvent {
  thread_id: string;
  total?: number;
  has_real_sessions?: boolean;
}

export interface TimelineSessionStreamHandlers {
  onConnected?: () => void;
  onHeartbeat?: (timestamp: string) => void;
  onSessionUpsert?: (event: TimelineSessionUpsertEvent) => void;
  onSessionRemove?: (event: TimelineSessionRemoveEvent) => void;
  onError?: (error: Event) => void;
}

export interface TimelineSessionStreamOptions {
  skipInitialReplay?: boolean;
}

function dispatchTimelineStreamEvent(kind: string, payload: Record<string, unknown> = {}) {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new CustomEvent("longhouse:timeline-stream", { detail: { kind, ...payload } }));
}

// ---------------------------------------------------------------------------
// API Functions
// ---------------------------------------------------------------------------

export function getTimelineSessionAnchor(
  session: Pick<AgentSession, "timeline_anchor_at" | "last_activity_at" | "started_at">,
): string {
  return session.timeline_anchor_at || session.last_activity_at || session.started_at;
}

export function getTimelineCardAnchor(
  card: Pick<TimelineSessionCard, "timeline_anchor_at" | "head">,
): string {
  return card.timeline_anchor_at || getTimelineSessionAnchor(card.head);
}

function buildGroupedQueryTimelineCards(sessions: AgentSession[]): TimelineSessionCard[] {
  const cardsByThread = new Map<string, TimelineSessionCard>();
  const orderedThreadIds: string[] = [];

  for (const session of sessions) {
    const threadId = session.thread_root_session_id || session.id;
    const existing = cardsByThread.get(threadId);
    if (!existing) {
      orderedThreadIds.push(threadId);
      cardsByThread.set(threadId, {
        thread_id: threadId,
        timeline_anchor_at: getTimelineSessionAnchor(session),
        head: session,
        detail: session,
        root: session,
        continuation_count: session.thread_continuation_count || 1,
        started_origin_label: session.origin_label || session.environment,
        head_origin_label: session.origin_label || session.environment,
      });
      continue;
    }

    const sessionHasExplicitMatch =
      session.match_event_id != null || !!session.match_snippet || session.match_score != null;
    const currentDetailHasExplicitMatch =
      existing.detail.match_event_id != null ||
      !!existing.detail.match_snippet ||
      existing.detail.match_score != null;
    const nextDetail =
      sessionHasExplicitMatch && !currentDetailHasExplicitMatch
        ? session
        : existing.detail;
    const nextHead =
      session.id === existing.detail.thread_head_session_id || session.is_writable_head
        ? session
        : existing.head;
    const nextRoot =
      session.id === existing.detail.thread_root_session_id
        ? session
        : existing.root;

    cardsByThread.set(threadId, {
      ...existing,
      head: nextHead,
      detail: nextDetail,
      root: nextRoot,
      continuation_count: Math.max(existing.continuation_count, session.thread_continuation_count || 1),
      started_origin_label:
        (session.id === existing.detail.thread_root_session_id
          ? session.origin_label || session.environment
          : existing.started_origin_label),
      head_origin_label:
        (session.id === existing.detail.thread_head_session_id || session.is_writable_head
          ? session.origin_label || session.environment
          : existing.head_origin_label),
    });
  }

  return orderedThreadIds
    .map((threadId) => cardsByThread.get(threadId))
    .filter((card): card is TimelineSessionCard => card != null);
}

/**
 * List agent sessions with optional filters.
 */
export async function fetchAgentSessions(
  filters: AgentSessionFilters = {},
): Promise<TimelineSessionsListResponse> {
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

  const groupedQueryMode = !!filters.query || (filters.mode != null && filters.mode !== "lexical");
  if (groupedQueryMode) {
    // Query-driven search still comes back as raw session hits today; collapse
    // those client-side until thread-aware query paging/ranking is designed cleanly.
    const rawResponse = await request<AgentSessionsListResponse>(path, { method: "GET" });
    return {
      sessions: buildGroupedQueryTimelineCards(rawResponse.sessions),
      total: rawResponse.total,
      has_real_sessions: rawResponse.has_real_sessions,
      query_grouping_mode: "grouped_results",
      query_grouping_has_more: (filters.offset || 0) + rawResponse.sessions.length < rawResponse.total,
      query_grouping_source_count: rawResponse.sessions.length,
    };
  }

  return request<TimelineSessionsListResponse>(path, { method: "GET" });
}

function buildTimelineSessionsParams(filters: AgentSessionFilters = {}): URLSearchParams {
  const params = new URLSearchParams();

  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.environment) params.set("environment", filters.environment);
  if (filters.device_id) params.set("device_id", filters.device_id);
  if (filters.days_back) params.set("days_back", String(filters.days_back));
  if (filters.query) params.set("query", filters.query);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  if (filters.mode && filters.mode !== "lexical") params.set("mode", filters.mode);
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.hide_autonomous === false) params.set("hide_autonomous", "false");

  return params;
}

function parseStreamEventData<T>(event: MessageEvent): T | null {
  try {
    return JSON.parse(event.data) as T;
  } catch {
    return null;
  }
}

export function connectTimelineSessionsStream(
  filters: AgentSessionFilters = {},
  handlers: TimelineSessionStreamHandlers = {},
  options: TimelineSessionStreamOptions = {},
): () => void {
  const params = buildTimelineSessionsParams(filters);
  if (options.skipInitialReplay) {
    params.set("skip_initial_replay", "true");
  }
  const commisId = typeof window !== "undefined" ? window.__TEST_COMMIS_ID__ : undefined;
  if (commisId !== undefined) {
    params.set("commis", String(commisId));
  }
  const queryString = params.toString();
  const url = buildUrl(`${TIMELINE_SESSIONS_PREFIX}/stream${queryString ? `?${queryString}` : ""}`);
  const eventSource = new EventSource(url, { withCredentials: true });

  eventSource.addEventListener("connected", () => {
    dispatchTimelineStreamEvent("connected");
    handlers.onConnected?.();
  });

  eventSource.addEventListener("heartbeat", (event: MessageEvent) => {
    const data = parseStreamEventData<{ timestamp: string }>(event);
    dispatchTimelineStreamEvent("heartbeat", { timestamp: data?.timestamp });
    if (data?.timestamp) {
      handlers.onHeartbeat?.(data.timestamp);
    }
  });

  eventSource.addEventListener("session_upsert", (event: MessageEvent) => {
    const data = parseStreamEventData<TimelineSessionUpsertEvent>(event);
    if (data?.session) {
      dispatchTimelineStreamEvent("session_upsert", {
        session_id: data.session.head?.id ?? data.session.thread_id,
      });
      handlers.onSessionUpsert?.(data);
    }
  });

  eventSource.addEventListener("session_remove", (event: MessageEvent) => {
    const data = parseStreamEventData<TimelineSessionRemoveEvent>(event);
    if (data?.thread_id) {
      dispatchTimelineStreamEvent("session_remove", { thread_id: data.thread_id });
      handlers.onSessionRemove?.(data);
    }
  });

  eventSource.onerror = (error) => {
    handlers.onError?.(error);
  };

  return () => {
    eventSource.close();
  };
}

// ---------------------------------------------------------------------------
// Session workspace SSE stream
// ---------------------------------------------------------------------------

export interface SessionWorkspaceStreamConnected {
  session_id: string;
  server_now_ms?: number;
}

export interface SessionWorkspaceStreamChange {
  session_id: string;
  latest_event_id: number;
  thread_session_count: number;
  detect_ms?: number;
  latest_event_emitted_at_ms?: number | null;
  server_fanout_at_ms?: number | null;
  server_now_ms?: number;
  pubsub_seq?: number;
  transcript_preview?: SessionTranscriptPreview | null;
}

export interface SessionWorkspaceStreamHandlers {
  onConnected?: (data: SessionWorkspaceStreamConnected) => void;
  onWorkspaceChanged?: (data: SessionWorkspaceStreamChange) => void;
  onHeartbeat?: (timestamp: string) => void;
  onError?: (error: Event) => void;
}

/**
 * Connect to a per-session SSE stream that fires on any workspace-visible
 * mutation (ingest, presence, runtime, session actions).  Returns a cleanup
 * function that closes the EventSource.
 */
export function connectSessionWorkspaceStream(
  sessionId: string,
  handlers: SessionWorkspaceStreamHandlers = {},
  options: { skipInitial?: boolean } = {},
): () => void {
  const params = new URLSearchParams();
  if (options.skipInitial) {
    params.set("skip_initial", "true");
  }
  const queryString = params.toString();
  const url = buildUrl(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/workspace/stream${queryString ? `?${queryString}` : ""}`,
  );
  const eventSource = new EventSource(url, { withCredentials: true });

  eventSource.addEventListener("connected", (event: MessageEvent) => {
    const data = parseStreamEventData<SessionWorkspaceStreamConnected>(event);
    dispatchTimelineStreamEvent("workspace_connected", {
      session_id: data?.session_id ?? sessionId,
      server_now_ms: data?.server_now_ms,
      client_received_at_ms: Date.now(),
    });
    handlers.onConnected?.(data ?? { session_id: sessionId });
  });

  eventSource.addEventListener("workspace_changed", (event: MessageEvent) => {
    const data = parseStreamEventData<SessionWorkspaceStreamChange>(event);
    if (data) {
      dispatchTimelineStreamEvent("workspace_changed", {
        session_id: data.session_id,
        latest_event_id: data.latest_event_id,
        latest_event_emitted_at_ms: data.latest_event_emitted_at_ms ?? null,
        server_fanout_at_ms: data.server_fanout_at_ms ?? null,
        server_now_ms: data.server_now_ms,
        pubsub_seq: data.pubsub_seq,
        client_received_at_ms: Date.now(),
        has_transcript_preview: Object.prototype.hasOwnProperty.call(data, "transcript_preview"),
        transcript_preview_event_id: data.transcript_preview?.event_id ?? null,
        transcript_preview_origin: data.transcript_preview?.event_origin ?? null,
        transcript_preview_text_length: data.transcript_preview?.text?.length ?? null,
      });
      handlers.onWorkspaceChanged?.(data);
    }
  });

  eventSource.addEventListener("heartbeat", (event: MessageEvent) => {
    const data = parseStreamEventData<{ timestamp: string }>(event);
    if (data?.timestamp) {
      handlers.onHeartbeat?.(data.timestamp);
    }
  });

  eventSource.onerror = (error) => {
    handlers.onError?.(error);
  };

  return () => {
    eventSource.close();
  };
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
    anchor?: "start" | "tail";
    branch_mode?: "head" | "all";
  } = {},
): Promise<AgentSessionProjectionResponse> {
  const params = new URLSearchParams();

  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  if (options.anchor && options.anchor !== "start") params.set("anchor", options.anchor);
  if (options.branch_mode) params.set("branch_mode", options.branch_mode);

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/projection${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionProjectionResponse>(path, {
    method: "GET",
    cache: "no-store",
  });
}

export async function fetchAgentSessionWorkspace(
  sessionId: string,
  options: {
    limit?: number;
    branch_mode?: "head" | "all";
  } = {},
): Promise<AgentSessionWorkspaceResponse> {
  const params = new URLSearchParams();

  if (options.limit) params.set("limit", String(options.limit));
  if (options.branch_mode) params.set("branch_mode", options.branch_mode);

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/workspace${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionWorkspaceResponse>(path, {
    method: "GET",
    cache: "no-store",
  });
}

export async function fetchAgentSessionTurns(
  sessionId: string,
  options: {
    limit?: number;
    offset?: number;
    order?: "asc" | "desc";
  } = {},
): Promise<AgentSessionTurnsListResponse> {
  const params = new URLSearchParams();

  if (options.limit != null) params.set("limit", String(options.limit));
  if (options.offset != null) params.set("offset", String(options.offset));
  if (options.order) params.set("order", options.order);

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/turns${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionTurnsListResponse>(path, { method: "GET" });
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
