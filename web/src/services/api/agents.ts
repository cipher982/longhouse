/**
 * Browser timeline/session archive API service functions.
 *
 * These routes back the cookie-authenticated browser session archive UI.
 * Device-token ingest and machine workflows stay on `/api/agents/*`.
 */

import { buildUrl, request } from "./base";
import type { components } from "../../generated/openapi-types";

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
  session_state: SessionStateFacts;
  runtime_display: SessionRuntimeDisplay;
  timeline_card: TimelineCardPresentation;
  transcript_preview?: SessionTranscriptPreview | null;
  user_messages: number;
  assistant_messages: number;
  tool_calls: number;
  summary: string | null;
  summary_title: string | null;
  /** Frozen, write-once headline; stable across the session's life. */
  anchor_title?: string | null;
  /** The provider's latest away recap, when it wrote one. */
  recap?: { text: string; at: string } | null;
  /** The most recent turn the provider reported as finished. */
  last_turn?: AgentSessionLastTurn | null;
  /** Model, effort and context size from the provider's last turn-ending response. */
  usage_latest?: {
    model?: string | null;
    effort?: string | null;
    context_tokens: number;
    /** The model's context window when the provider reports it (Codex does). */
    context_window?: number | null;
    output_tokens: number;
    thinking_tokens?: number | null;
    at: string;
  } | null;
  /** Server-resolved headline to render verbatim (no client fallback ladder). */
  timeline_title?: string | null;
  summary_status?:
    | "ready"
    | "pending"
    | "failed"
    | "unavailable"
    | (string & {})
    | null;
  first_user_message: string | null;
  match_event_id?: AgentEventId | null;
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
  user_state?: string;
  user_hidden_from_timeline?: boolean;
  execution_lifetime?: "one_shot" | "live_control" | null;
  /**
   * Attribution for the user whose signed share link or legacy
   * `?shared_by=<id>` URL surfaced this session. The server hides this when
   * the sharer is the current viewer (self-share).
   */
  sharer?: SessionSharer | null;
}

export type SessionStateFacts = components["schemas"]["SessionStateFacts"];

export interface SessionSharer {
  id: number;
  display_name: string | null;
}

export interface CreateSessionShareRequest {
  expires_in_days?: number | null;
  note?: string | null;
}

export interface SessionShareResponse {
  id: number;
  session_id: string;
  token: string;
  share_url: string;
  expires_at: string | null;
  revoked_at: string | null;
  sharer: SessionSharer | null;
}

export interface SessionSharePreviewResponse {
  provider: string;
  device_name: string | null;
  started_at: string | null;
  ended_at: string | null;
  expires_at: string | null;
  note: string | null;
  sharer: SessionSharer | null;
}

export interface SessionShareResolveResponse {
  session_id: string;
  share_id: number;
  expires_at: string | null;
  note: string | null;
  sharer: SessionSharer | null;
}

export interface SessionTranscriptPreview {
  event_id: number;
  text: string;
  role?: string;
  tool_name: string | null;
  /** Provider-native tool input; free-form tools may emit a JSON string. */
  tool_input_json?: unknown;
  tool_output_text?: string | null;
  tool_call_id?: string | null;
  tool_call_state?: "running" | "completed" | "dropped" | null;
  event_origin: string;
  timestamp?: string | null;
  is_provisional: boolean;
  is_complete: boolean;
  content_cursor?: string | null;
  is_stale: boolean;
  stale_reason?:
    | "freshness_window_expired"
    | "missing_preview_timestamp"
    | "superseded_by_durable"
    | null;
}

export type RuntimeSignalTier = components["schemas"]["SignalTier"];
export type RuntimeTone = components["schemas"]["Tone"];
export type RuntimeControlPath = components["schemas"]["ControlPath"];
export type RuntimeActivityRecency = components["schemas"]["ActivityRecency"];
export type RuntimeLifecycle = components["schemas"]["Lifecycle"];
export type RuntimeHostState = components["schemas"]["HostState"];
export type RuntimeTerminalReason = components["schemas"]["TerminalReason"];
export type AgentEventMediaRef = components["schemas"]["EventMediaRefResponse"];
export type SessionPauseQuestionOption =
  components["schemas"]["SessionPauseQuestionOptionResponse"];
export type SessionPauseQuestion =
  components["schemas"]["SessionPauseQuestionResponse"];
export type SessionPauseRequest =
  components["schemas"]["SessionPauseRequestProjectionResponse"];
export type PauseRequestResponseRequest =
  components["schemas"]["PauseRequestResponseRequest"];
export type PauseRequestResponseResponse =
  components["schemas"]["PauseRequestResponseResponse"];

export type SessionRuntimeDisplay =
  components["schemas"]["SessionRuntimeDisplayResponse"];

export interface TimelineBadgePresentation {
  label: string;
  tone:
    | "neutral"
    | "inactive"
    | "active"
    | "thinking"
    | "running"
    | "blocked"
    | "stalled"
    | "idle"
    | "closed"
    | (string & {});
}

export interface TimelineStatusPresentation extends TimelineBadgePresentation {
  seen_at: string | null;
  seen_at_prefix: string;
}

export interface TimelineCardPresentation {
  ownership: TimelineBadgePresentation;
  status: TimelineStatusPresentation;
  border_tone:
    | "inactive"
    | "active"
    | "thinking"
    | "running"
    | "blocked"
    | "stalled"
    | "idle"
    | "closed"
    | (string & {});
}

export interface SessionControl {
  source_runner_id: number | null;
  source_runner_name: string | null;
  attach_command?: string | null;
}

export type SendDisabledReason =
  | "session_closed"
  | "control_offline"
  | "input_not_supported"
  | "read_only";

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
  control_label?:
    | "live"
    | "reattach"
    | "console"
    | "search-only"
    | "imported"
    | null;
  observe_only?: boolean;
  search_only?: boolean;
  staleness_reason?: string | null;
  can_send_input?: boolean;
  can_interrupt?: boolean;
  can_terminate?: boolean;
  can_tail_output?: boolean;
  can_resume?: boolean;
  turn_state?: "idle" | "queued" | "starting" | "active" | "draining";
  can_start_turn?: boolean;
  can_interrupt_active_turn?: boolean;
  /**
   * True when this session accepts image attachments on input. Today this is
   * codex_app_server + live_control_available; the server is the source of
   * truth so the web client doesn't have to know the transport set.
   */
  attach_images?: boolean;
}

export interface SessionResumeIntent {
  session_id: string;
  provider: string;
  machine_id: string | null;
  machine_label: string | null;
  cwd: string | null;
  available: boolean;
  reason: string | null;
  argv: string[];
  command: string | null;
  handoff: "terminal_command";
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

/** One dynamic-workflow run whose subagent threads live under a session. */
export interface WorkflowRunSummary {
  workflow_run_id: string;
  agent_count: number;
  skill: string | null;
}

export interface SessionWorkflowRunsResponse {
  session_id: string;
  workflow_runs: WorkflowRunSummary[];
}

/** One subagent within a dynamic-workflow run. */
export interface WorkflowRunAgent {
  thread_id: string;
  session_id: string;
  is_primary: boolean;
  branch_kind: string | null;
  agent_id: string | null;
  attribution_agent: string | null;
  attribution_skill: string | null;
  source_path: string | null;
}

export interface WorkflowRunResponse {
  workflow_run_id: string;
  skill: string | null;
  parent_session_id: string | null;
  agent_count: number;
  agents: WorkflowRunAgent[];
}

export interface AgentSessionTranscriptAction {
  id: string;
  kind: "turn_interrupted" | string;
  provider?: string | null;
  source:
    | "user"
    | "remote_control"
    | "provider"
    | "system"
    | "unknown"
    | string;
  provider_reason?: string | null;
  event_id?: number | null;
}

export interface AgentSessionProjectionItem {
  kind: "event" | "seam" | "action";
  session_id: string;
  timestamp: string;
  event?: AgentEvent | null;
  action?: AgentSessionTranscriptAction | null;
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
  generation_id?: string | null;
  next_cursor?: string | null;
  has_more?: boolean;
}

export interface AgentSessionWorkspaceRevision {
  latest_event_id?: AgentEventId | null;
  latest_session_updated_at?: string | null;
  latest_runtime_signal_at?: string | null;
  runtime_version_sum?: number;
  pause_request_count?: number;
  pause_request_fingerprint?: string | null;
  managed_control_count?: number;
  managed_control_fingerprint?: string | null;
  live_preview_updated_at?: string | null;
  thread_session_count?: number;
  fingerprint: string;
}

export interface AgentSessionWorkspaceResponse {
  session: AgentSession;
  thread: AgentSessionThreadResponse;
  projection: AgentSessionProjectionResponse;
  workspace_revision: AgentSessionWorkspaceRevision;
  /** The provider has live managed control but does not expose transcript data yet. */
  control_only?: boolean;
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

export type PresenceState = components["schemas"]["PresenceState"];

export type UserStateAction = "park" | "snooze" | "archive" | "resume";
export interface AgentEventTurnEnd {
  duration_ms: number;
  ended_at: string;
  message_count?: number | null;
  /** Codex reports a stopped turn's duration too; Claude only writes finished ones. */
  outcome?: "completed" | "aborted";
}

export interface AgentSessionLastTurn {
  duration_ms: number;
  ended_at: string;
  event_id?: string | null;
  outcome?: "completed" | "aborted";
}

export interface AgentEventInputOrigin {
  authored_via: "longhouse" | "terminal";
  session_input_id?: number | null;
  client_request_id?: string | null;
}

export type AgentEventId = string | number;

export type ToolPresentationDisposition =
  | "exact"
  | "parsed"
  | "generic"
  | "unknown"
  | "invalid";
export type ToolPresentationTier = "noise" | "context" | "action";
export type ToolPresentationAggregate = "search" | "read" | "list" | "wait";

export interface AgentShellSummaryOperation {
  key: string;
  label: string;
  executable: string;
  subcommands: string[];
  count: number;
}

export interface AgentShellCommandSummary {
  version: number;
  confidence: "syntactic" | "partial" | "opaque";
  operations: AgentShellSummaryOperation[];
  candidate_count: number;
  truncated: boolean;
  dynamic: boolean;
  parse_error?: string | null;
  parser_id: string;
  shape_registry_version: number;
}

export interface AgentToolPresentationChild {
  version: number;
  child_id: string;
  disposition: ToolPresentationDisposition;
  tool_name: string;
  label: string;
  icon: string;
  color: string;
  tier: ToolPresentationTier;
  aggregate?: ToolPresentationAggregate | null;
  mcp_namespace?: string | null;
  tool_input_json?: unknown;
  rule_id: string;
  source_span?: number[];
  input_complete?: boolean;
  result_forwarded?: boolean;
}

export interface AgentToolPresentation {
  version: number;
  disposition: ToolPresentationDisposition;
  tool_name: string;
  source_tool_name: string;
  execution_method?: string | null;
  label: string;
  icon: string;
  color: string;
  tier: ToolPresentationTier;
  aggregate?: ToolPresentationAggregate | null;
  mcp_namespace?: string | null;
  tool_input_json?: unknown;
  rule_id: string;
  wrapper_recedes: boolean;
  children?: AgentToolPresentationChild[];
  shell_summary?: AgentShellCommandSummary | null;
}

export interface AgentEvent {
  id: AgentEventId;
  cursor?: string | null;
  role: string;
  content_text: string | null;
  /** Parser-owned semantic kind for provider-authored status rows. */
  interaction_kind?: string | null;
  raw_content_text?: string | null;
  input_origin?: AgentEventInputOrigin | null;
  /** Provider turn accounting stamped on the event the turn ended on. */
  turn_end?: AgentEventTurnEnd | null;
  tool_name?: string | null;
  /** Provider-native tool input; free-form tools may emit a JSON string. */
  tool_input_json: unknown;
  tool_output_text: string | null;
  tool_call_id: string | null;
  tool_presentation?: AgentToolPresentation | null;
  tool_call_state?: "running" | "completed" | "dropped" | null;
  timestamp: string;
  in_active_context?: boolean;
  branch_id?: number | null;
  is_head_branch?: boolean;
  media_refs?: AgentEventMediaRef[];
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

function dispatchTimelineStreamEvent(
  kind: string,
  payload: Record<string, unknown> = {},
) {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(
    new CustomEvent("longhouse:timeline-stream", {
      detail: { kind, ...payload },
    }),
  );
}

// ---------------------------------------------------------------------------
// API Functions
// ---------------------------------------------------------------------------

export function getTimelineSessionAnchor(
  session: Pick<
    AgentSession,
    "timeline_anchor_at" | "last_activity_at" | "started_at"
  >,
): string {
  return (
    session.timeline_anchor_at || session.last_activity_at || session.started_at
  );
}

export function getTimelineCardAnchor(
  card: Pick<TimelineSessionCard, "timeline_anchor_at" | "head">,
): string {
  return card.timeline_anchor_at || getTimelineSessionAnchor(card.head);
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

  const groupedQueryMode =
    !!filters.query || (filters.mode != null && filters.mode !== "lexical");
  if (groupedQueryMode) {
    const response = await request<TimelineSessionsListResponse>(path, {
      method: "GET",
    });
    return {
      ...response,
      query_grouping_mode: "grouped_results",
      query_grouping_has_more:
        (filters.offset || 0) + response.sessions.length < response.total,
      query_grouping_source_count: response.sessions.length,
    };
  }

  return request<TimelineSessionsListResponse>(path, { method: "GET" });
}

function buildTimelineSessionsParams(
  filters: AgentSessionFilters = {},
): URLSearchParams {
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
  const workerId =
    typeof window !== "undefined" ? window.__TEST_WORKER_ID__ : undefined;
  if (workerId !== undefined) {
    params.set("worker", String(workerId));
  }
  const queryString = params.toString();
  const url = buildUrl(
    `${TIMELINE_SESSIONS_PREFIX}/stream${queryString ? `?${queryString}` : ""}`,
  );
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
      dispatchTimelineStreamEvent("session_remove", {
        thread_id: data.thread_id,
      });
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

export interface SessionWorkspaceStreamReplayGap {
  session_id: string;
  requested_seq: number;
  earliest_seq: number | null;
  latest_seq: number;
  reason: string;
}

export interface SessionWorkspaceStreamChange {
  session_id: string;
  change_kind?: string | null;
  latest_event_id: number;
  thread_session_count: number;
  detect_ms?: number;
  latest_event_emitted_at_ms?: number | null;
  server_fanout_at_ms?: number | null;
  server_now_ms?: number;
  catalog_commit_seq?: number | null;
  pubsub_seq?: number;
  transcript_preview?: SessionTranscriptPreview | null;
}

export interface SessionWorkspaceStreamHandlers {
  onConnected?: (data: SessionWorkspaceStreamConnected) => void;
  onReplayGap?: (data: SessionWorkspaceStreamReplayGap) => void;
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
  options: {
    skipInitial?: boolean;
    knownWorkspaceFingerprint?: string | null;
  } = {},
): () => void {
  const params = new URLSearchParams();
  if (options.skipInitial) {
    params.set("skip_initial", "true");
  }
  if (options.knownWorkspaceFingerprint) {
    params.set(
      "known_workspace_fingerprint",
      options.knownWorkspaceFingerprint,
    );
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
        change_kind: data.change_kind ?? null,
        latest_event_id: data.latest_event_id,
        latest_event_emitted_at_ms: data.latest_event_emitted_at_ms ?? null,
        server_fanout_at_ms: data.server_fanout_at_ms ?? null,
        server_now_ms: data.server_now_ms,
        catalog_commit_seq: data.catalog_commit_seq ?? null,
        pubsub_seq: data.pubsub_seq,
        client_received_at_ms: Date.now(),
        has_transcript_preview: Object.prototype.hasOwnProperty.call(
          data,
          "transcript_preview",
        ),
        transcript_preview_event_id: data.transcript_preview?.event_id ?? null,
        transcript_preview_origin:
          data.transcript_preview?.event_origin ?? null,
        transcript_preview_text_length:
          data.transcript_preview?.text?.length ?? null,
      });
      handlers.onWorkspaceChanged?.(data);
    }
  });

  eventSource.addEventListener("replay_gap", (event: MessageEvent) => {
    const data = parseStreamEventData<SessionWorkspaceStreamReplayGap>(event);
    if (data) {
      handlers.onReplayGap?.(data);
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

/** Re-check ended Helm Resume eligibility and return its terminal handoff. */
export async function createSessionResumeIntent(
  sessionId: string,
): Promise<SessionResumeIntent> {
  return request<SessionResumeIntent>(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/resume-intent`,
    { method: "POST" },
  );
}

/** List the dynamic-workflow runs whose subagent threads live under a session.
 * Browser-cookie-authenticated via the /timeline router (NOT /agents, which is
 * machine-token auth). */
/** One worker transcript a tool call spawned. */
export interface SessionSubagent {
  session_id: string;
  provider: string;
  parent_tool_call_id: string | null;
  run_id: string | null;
  started_at: string | null;
  last_activity_at: string | null;
  ended_at: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_calls: number;
  title: string | null;
  first_user_message_preview: string | null;
  last_visible_text_preview: string | null;
}

export interface SessionSubagentsResponse {
  session_id: string;
  children: SessionSubagent[];
}

/**
 * Workers this session spawned. They are hidden from the timeline by design —
 * a subagent is a turn artifact, not a session — so this is the route that
 * makes them reachable from the work they belong to.
 */
export async function fetchSessionSubagents(
  sessionId: string,
): Promise<SessionSubagentsResponse> {
  return request<SessionSubagentsResponse>(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/subagents`,
    { method: "GET" },
  );
}

export async function fetchSessionWorkflowRuns(
  sessionId: string,
): Promise<SessionWorkflowRunsResponse> {
  return request<SessionWorkflowRunsResponse>(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/workflows`,
    { method: "GET" },
  );
}

/** Fetch one dynamic-workflow run with its individual subagent threads. */
export async function fetchWorkflowRun(
  workflowRunId: string,
): Promise<WorkflowRunResponse> {
  return request<WorkflowRunResponse>(
    `${TIMELINE_API_PREFIX}/workflows/${workflowRunId}`,
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
    cursor?: string | null;
  } = {},
): Promise<AgentSessionProjectionResponse> {
  const params = new URLSearchParams();

  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  if (options.anchor && options.anchor !== "start")
    params.set("anchor", options.anchor);
  if (options.branch_mode) params.set("branch_mode", options.branch_mode);
  if (options.cursor) params.set("cursor", options.cursor);

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
    shared_by?: number | null;
    share_token?: string | null;
  } = {},
): Promise<AgentSessionWorkspaceResponse> {
  const params = new URLSearchParams();

  if (options.limit) params.set("limit", String(options.limit));
  if (options.branch_mode) params.set("branch_mode", options.branch_mode);
  if (options.shared_by !== undefined && options.shared_by !== null) {
    params.set("shared_by", String(options.shared_by));
  }
  if (options.share_token) {
    params.set("share_token", options.share_token);
  }

  const queryString = params.toString();
  const path = `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/workspace${queryString ? `?${queryString}` : ""}`;

  return request<AgentSessionWorkspaceResponse>(path, {
    method: "GET",
    cache: "no-store",
  });
}

export async function createSessionShare(
  sessionId: string,
  body: CreateSessionShareRequest = {},
): Promise<SessionShareResponse> {
  return request<SessionShareResponse>(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/shares`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export async function revokeSessionShare(
  shareId: number,
): Promise<SessionShareResolveResponse> {
  return request<SessionShareResolveResponse>(
    `${TIMELINE_API_PREFIX}/session-shares/${shareId}`,
    { method: "DELETE" },
  );
}

export async function resolveSessionShare(
  token: string,
): Promise<SessionShareResolveResponse> {
  return request<SessionShareResolveResponse>(
    `${TIMELINE_API_PREFIX}/session-shares/${encodeURIComponent(token)}/resolve`,
    {
      method: "GET",
      cache: "no-store",
    },
  );
}

export async function fetchSessionSharePreview(
  token: string,
): Promise<SessionSharePreviewResponse> {
  return request<SessionSharePreviewResponse>(
    `/public/session-shares/${encodeURIComponent(token)}/preview`,
    {
      method: "GET",
      cache: "no-store",
    },
  );
}

export async function respondToPauseRequest(
  sessionId: string,
  pauseRequestId: string,
  body: PauseRequestResponseRequest,
): Promise<PauseRequestResponseResponse> {
  return request<PauseRequestResponseResponse>(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/pause-requests/${pauseRequestId}/response`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
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

export type RecallSearchResult = components["schemas"]["RecallSearchResult"];
export type RecallExpandedTurn = components["schemas"]["RecallExpandedTurn"];
export type RecallResponse = components["schemas"]["RecallResponse"];
export type RecallCoverageSummary =
  components["schemas"]["RecallCoverageSummary"];
export type RecallContextResponse =
  components["schemas"]["RecallContextResponse"];

export interface RecallFilters {
  query: string;
  project?: string;
  provider?: string;
  mode?: "auto" | "lexical" | "semantic";
  since_days?: number;
  max_results?: number;
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
 * Recall: compact turn-level search cards.
 */
export async function fetchRecall(
  filters: RecallFilters,
): Promise<RecallResponse> {
  const params = new URLSearchParams();
  params.set("query", filters.query);
  if (filters.project) params.set("project", filters.project);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.mode) params.set("mode", filters.mode);
  if (filters.since_days) params.set("since_days", String(filters.since_days));
  if (filters.max_results)
    params.set("max_results", String(filters.max_results));
  return request<RecallResponse>(
    `${TIMELINE_API_PREFIX}/recall?${params.toString()}`,
    { method: "GET" },
  );
}

/** Open one recall result under the server's fixed total content budget. */
export async function fetchRecallContext(
  ref: string,
  options: { before?: number; after?: number; max_content_bytes?: number } = {},
): Promise<RecallContextResponse> {
  const params = new URLSearchParams({ ref });
  if (options.before !== undefined)
    params.set("before", String(options.before));
  if (options.after !== undefined) params.set("after", String(options.after));
  if (options.max_content_bytes !== undefined)
    params.set("max_content_bytes", String(options.max_content_bytes));
  return request<RecallContextResponse>(
    `${TIMELINE_API_PREFIX}/recall/context?${params.toString()}`,
    { method: "GET" },
  );
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

export async function markSessionRead(
  sessionId: string,
  readThrough: string,
): Promise<{ session_id: string; last_read_at: string | null }> {
  return request(`${TIMELINE_SESSIONS_PREFIX}/${sessionId}/read`, {
    method: "POST",
    body: JSON.stringify({ read_through: readThrough }),
  });
}

export async function setSessionTimelineVisibility(
  sessionId: string,
  hidden: boolean,
): Promise<{ session_id: string; hidden: boolean }> {
  return request(
    `${TIMELINE_SESSIONS_PREFIX}/${sessionId}/timeline-visibility`,
    {
      method: "PATCH",
      body: JSON.stringify({ hidden }),
    },
  );
}

export interface SessionBranchRequest {
  message: string;
  client_request_id: string;
  display_name?: string | null;
  launch_surface?: string;
}

export interface SessionBranchResponse {
  session_id: string;
  thread_id: string;
  turn_id: string;
  run_id: string | null;
  state: string;
  created: boolean;
}

/** Branch an ended session into a new one that continues its conversation. */
export async function createSessionBranch(
  sessionId: string,
  body: SessionBranchRequest,
): Promise<SessionBranchResponse> {
  return request<SessionBranchResponse>(`/sessions/${sessionId}/branches`, {
    method: "POST",
    body: JSON.stringify({ launch_surface: "web", ...body }),
  });
}
