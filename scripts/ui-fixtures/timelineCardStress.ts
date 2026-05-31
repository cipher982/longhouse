type SessionCapabilities = {
  live_control_available: boolean;
  host_reattach_available: boolean;
  reply_to_live_session_available: boolean;
};

type SessionLivenessFacts = {
  control_path: "managed" | "unmanaged";
  process_state: "running" | "closed" | "unknown";
  host: {
    state: "online" | "stale" | "offline" | "unknown";
    last_seen_at?: string | null;
    source?: string | null;
  };
  process: {
    status: "observed" | "not_observed" | "unknown";
    pid?: number | null;
    process_start_time?: string | null;
    observed_at?: string | null;
    last_seen_at?: string | null;
    source_mtime?: string | null;
    source_path?: string | null;
    reason?: string | null;
    source?: string | null;
  };
  phase: {
    kind?: string | null;
    tool?: string | null;
    source?: string | null;
    observed_at?: string | null;
    expires_at?: string | null;
  };
  activity: {
    last_transcript_at?: string | null;
    last_runtime_signal_at?: string | null;
    last_progress_at?: string | null;
  };
  lifecycle: {
    state: "open" | "closed" | "unknown";
    reason?: string | null;
    observed_at?: string | null;
  };
};

type AgentSession = {
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
  timeline_anchor_at: string | null;
  runtime_phase?: string | null;
  phase_started_at?: string | null;
  last_progress_at?: string | null;
  runtime_source?: string | null;
  terminal_state?: string | null;
  runtime_version?: number | null;
  status?: string | null;
  presence_state?: string | null;
  presence_tool?: string | null;
  presence_updated_at?: string | null;
  last_live_at?: string | null;
  display_phase?: string | null;
  active_tool?: string | null;
  confidence?: string | null;
  runtime_facts?: SessionLivenessFacts | null;
  transcript_preview?: {
    event_id: number;
    text: string;
    event_origin: string;
    timestamp: string;
    is_provisional: boolean;
    is_complete: boolean;
    content_cursor?: string | null;
    is_stale: boolean;
    stale_reason?: "freshness_window_expired" | "missing_preview_timestamp" | "superseded_by_durable" | null;
  } | null;
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
  home_label: string | null;
  branched_from_event_id: number | null;
  is_writable_head: boolean;
  control: {
    managed_transport: "claude_channel_bridge" | "codex_app_server" | null;
    source_runner_id: number | null;
    source_runner_name: string | null;
    attach_command?: string | null;
  } | null;
  capabilities: SessionCapabilities;
  loop_mode: "manual" | "assist" | "autopilot";
  user_state?: string;
};

type TimelineSessionCard = {
  thread_id: string;
  timeline_anchor_at: string | null;
  head: AgentSession;
  detail: AgentSession;
  root: AgentSession;
  continuation_count: number;
  started_origin_label: string | null;
  head_origin_label: string | null;
};

type TimelineSessionsListResponse = {
  sessions: TimelineSessionCard[];
  total: number;
  has_real_sessions: boolean;
};

type AgentFiltersResponse = {
  projects: string[];
  providers: string[];
  machines: string[];
};

function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    live_control_available: false,
    host_reattach_available: false,
    reply_to_live_session_available: false,
    ...overrides,
  };
}

function makeRuntimeFacts(overrides: Partial<SessionLivenessFacts> = {}): SessionLivenessFacts {
  return {
    control_path: "unmanaged",
    process_state: "unknown",
    host: {
      state: "unknown",
      last_seen_at: null,
      source: null,
    },
    process: {
      status: "unknown",
      pid: null,
      process_start_time: null,
      observed_at: null,
      last_seen_at: null,
      source_mtime: null,
      source_path: null,
      reason: null,
      source: null,
    },
    phase: {
      kind: null,
      tool: null,
      source: null,
      observed_at: null,
      expires_at: null,
    },
    activity: {
      last_transcript_at: null,
      last_runtime_signal_at: null,
      last_progress_at: null,
    },
    lifecycle: {
      state: "unknown",
      reason: null,
      observed_at: null,
    },
    ...overrides,
  };
}

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  const now = "2026-04-15T16:12:00Z";
  return {
    id: "session-1",
    provider: "codex",
    project: "zerg",
    device_id: "device-cinder",
    environment: "development",
    cwd: "/Users/example/git/zerg",
    git_repo: "https://github.com/cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-04-15T15:10:00Z",
    ended_at: null,
    last_activity_at: now,
    timeline_anchor_at: now,
    runtime_phase: null,
    phase_started_at: null,
    last_progress_at: now,
    runtime_source: null,
    terminal_state: null,
    runtime_version: 1,
    status: "completed",
    presence_state: null,
    presence_tool: null,
    presence_updated_at: null,
    last_live_at: null,
    display_phase: null,
    active_tool: null,
    confidence: null,
    runtime_facts: null,
    transcript_preview: null,
    user_messages: 10,
    assistant_messages: 10,
    tool_calls: 6,
    summary:
      "Completed timeline cleanup and follow-up fixes after reviewing mobile card composition.",
    summary_title: "Cleanup sessions page",
    first_user_message: "Clean up the sessions page card layout.",
    match_event_id: null,
    match_snippet: null,
    match_role: null,
    match_score: null,
    thread_root_session_id: "thread-1",
    thread_head_session_id: "thread-1",
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: null,
    origin_label: "cinder",
    home_label: "On this Mac",
    branched_from_event_id: null,
    is_writable_head: true,
    control: null,
    capabilities: makeCapabilities(),
    loop_mode: "manual",
    ...overrides,
  };
}

function makeTimelineCard(
  overrides: Partial<AgentSession> = {},
  cardOverrides: Partial<TimelineSessionCard> = {},
): TimelineSessionCard {
  const detail = makeSession(overrides);
  const head =
    cardOverrides.head ??
    makeSession({
      ...overrides,
      id: detail.thread_head_session_id || detail.id,
    });
  const root =
    cardOverrides.root ??
    makeSession({
      ...overrides,
      id: detail.thread_root_session_id || detail.id,
    });

  return {
    thread_id: detail.thread_root_session_id,
    timeline_anchor_at: detail.timeline_anchor_at || detail.last_activity_at || detail.started_at,
    head,
    detail,
    root,
    continuation_count: detail.thread_continuation_count,
    started_origin_label: root.origin_label || root.environment,
    head_origin_label: head.origin_label || head.environment,
    ...cardOverrides,
  };
}

export function buildTimelineCardStressFixture(): {
  sessions: TimelineSessionsListResponse;
  filters: AgentFiltersResponse;
  runners: { runners: [] };
} {
  const liveCodex = makeTimelineCard(
    {
      id: "live-codex-head",
      thread_root_session_id: "thread-live-codex",
      thread_head_session_id: "thread-live-codex",
      provider: "codex",
      project: "zerg",
      git_branch: "main",
      started_at: "2026-04-15T15:16:00Z",
      last_activity_at: "2026-04-15T16:11:35Z",
      timeline_anchor_at: "2026-04-15T16:11:35Z",
      user_messages: 12,
      assistant_messages: 12,
      tool_calls: 94,
      summary_title: "Secure Hybrid Auth Shipped: Web, iOS, and Deployments",
      summary:
        "Delivered unified hybrid auth architecture with /login route, refactored web components, and integrated iOS updates including post-review fixes.",
      transcript_preview: {
        event_id: 1001,
        text:
          "I have the provisional event preview visible on the timeline now; this should arrive before the slower durable transcript poll catches up.",
        event_origin: "live_provisional",
        timestamp: "2026-04-15T16:11:39Z",
        is_provisional: true,
        is_complete: false,
        content_cursor: "codex_bridge_live:live-codex-head:fixture-live-thread:fixture-live-turn:24",
        is_stale: false,
        stale_reason: null,
      },
      status: "working",
      presence_state: "running",
      presence_tool: "mcp__hatch__hatch_codex",
      active_tool: "mcp__hatch__hatch_codex",
      presence_updated_at: "2026-04-15T16:11:35Z",
      last_live_at: "2026-04-15T16:11:35Z",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Running mcp__hatch__hatch_codex",
      runtime_facts: makeRuntimeFacts({
        control_path: "managed",
        host: {
          state: "online",
          last_seen_at: "2026-04-15T16:11:35Z",
          source: "machine_heartbeat",
        },
        phase: {
          kind: "running",
          tool: "mcp__hatch__hatch_codex",
          source: "managed_local_transport",
          observed_at: "2026-04-15T16:11:35Z",
          expires_at: "2026-04-15T16:26:35Z",
        },
        activity: {
          last_transcript_at: "2026-04-15T16:11:35Z",
          last_runtime_signal_at: "2026-04-15T16:11:35Z",
          last_progress_at: null,
        },
        lifecycle: {
          state: "open",
          reason: "phase_observed",
          observed_at: "2026-04-15T16:11:35Z",
        },
      }),
      origin_label: "cinder",
      home_label: "On this Mac",
      capabilities: makeCapabilities({
        live_control_available: true,
        host_reattach_available: true,
        reply_to_live_session_available: true,
      }),
      control: {
        managed_transport: "codex_app_server",
        source_runner_id: null,
        source_runner_name: null,
        attach_command: "longhouse codex --attach live-codex-head",
      },
    },
    {
      head_origin_label: "cinder",
      started_origin_label: "cinder",
    },
  );

  const idleClaude = makeTimelineCard(
    {
      id: "idle-claude-head",
      thread_root_session_id: "thread-idle-claude",
      thread_head_session_id: "thread-idle-claude",
      provider: "claude",
      project: "zerg",
      git_branch: "main",
      started_at: "2026-04-15T15:05:00Z",
      last_activity_at: "2026-04-15T16:10:00Z",
      timeline_anchor_at: "2026-04-15T16:10:00Z",
      user_messages: 25,
      assistant_messages: 25,
      tool_calls: 90,
      summary_title: "Zerg Audits, Cleanup, TUI Implementation, and Install Fix",
      summary:
        "Completed audits of 16 docket items closing 10 and archiving others, deleted obsolete docker files, implemented SSO cleanup, and fixed install flow regressions.",
      status: "idle",
      presence_state: "idle",
      presence_updated_at: "2026-04-15T16:10:00Z",
      last_live_at: "2026-04-15T16:10:00Z",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Idle",
      runtime_facts: makeRuntimeFacts({
        control_path: "managed",
        host: {
          state: "online",
          last_seen_at: "2026-04-15T16:10:00Z",
          source: "machine_heartbeat",
        },
        phase: {
          kind: "idle",
          tool: null,
          source: "managed_local_transport",
          observed_at: "2026-04-15T16:10:00Z",
          expires_at: "2026-04-15T16:25:00Z",
        },
        activity: {
          last_transcript_at: "2026-04-15T16:10:00Z",
          last_runtime_signal_at: "2026-04-15T16:10:00Z",
          last_progress_at: null,
        },
        lifecycle: {
          state: "open",
          reason: "phase_observed",
          observed_at: "2026-04-15T16:10:00Z",
        },
      }),
      origin_label: "cinder",
      home_label: "On this Mac",
      capabilities: makeCapabilities({
        live_control_available: true,
        host_reattach_available: true,
        reply_to_live_session_available: true,
      }),
      control: {
        managed_transport: "claude_channel_bridge",
        source_runner_id: null,
        source_runner_name: null,
        attach_command: "longhouse claude --attach idle-claude-head",
      },
    },
    {
      head_origin_label: "cinder",
      started_origin_label: "cinder",
    },
  );

  const unmanagedCodex = makeTimelineCard(
    {
      id: "vpn-codex",
      thread_root_session_id: "thread-vpn-codex",
      thread_head_session_id: "thread-vpn-codex",
      provider: "codex",
      project: "demo-vpn",
      git_branch: "main",
      started_at: "2026-04-15T15:09:00Z",
      last_activity_at: "2026-04-15T16:09:00Z",
      timeline_anchor_at: "2026-04-15T16:09:00Z",
      user_messages: 18,
      assistant_messages: 18,
      tool_calls: 48,
      summary_title: "CLI-Driven Signed macOS VPN Tunnel Prototype",
      summary:
        "Implemented CLI mode in the native macOS app for terminal-based install, connect, disconnect, and status control of the Packet Tunnel VPN extension.",
      status: "working",
      runtime_source: "progress",
      confidence: "inferred",
      last_progress_at: "2026-04-15T16:09:00Z",
      display_phase: "Recent progress",
      runtime_facts: makeRuntimeFacts({
        control_path: "unmanaged",
        process_state: "running",
        host: {
          state: "online",
          last_seen_at: "2026-04-15T16:09:00Z",
          source: "machine_heartbeat",
        },
        process: {
          status: "observed",
          pid: 4321,
          process_start_time: "2026-04-15T15:09:00Z",
          observed_at: "2026-04-15T16:09:00Z",
          last_seen_at: "2026-04-15T16:09:00Z",
          source_mtime: "2026-04-15T16:09:00Z",
          source_path: "/Users/example/.codex/sessions/vpn-codex.jsonl",
          reason: null,
          source: "machine_process_scan",
        },
        activity: {
          last_transcript_at: "2026-04-15T16:09:00Z",
          last_runtime_signal_at: null,
          last_progress_at: "2026-04-15T16:09:00Z",
        },
        lifecycle: {
          state: "open",
          reason: "process_observed",
          observed_at: "2026-04-15T16:09:00Z",
        },
      }),
      origin_label: "cinder",
      home_label: null,
      capabilities: makeCapabilities(),
      control: null,
    },
    {
      head_origin_label: "cinder",
      started_origin_label: "cinder",
    },
  );

  const unmanagedClaude = makeTimelineCard(
    {
      id: "audit-claude-progress",
      thread_root_session_id: "thread-audit-claude-progress",
      thread_head_session_id: "thread-audit-claude-progress",
      provider: "claude",
      project: "project",
      git_branch: "main",
      started_at: "2026-04-15T15:08:00Z",
      last_activity_at: "2026-04-15T16:08:00Z",
      timeline_anchor_at: "2026-04-15T16:08:00Z",
      user_messages: 16,
      assistant_messages: 16,
      tool_calls: 41,
      summary_title: "Provider-Neutral Active Runtime Fixture",
      summary:
        "Keeps the same Active state visible on a Claude card so timeline QA catches provider-specific color or icon regressions.",
      status: "active",
      runtime_source: "progress",
      confidence: "inferred",
      last_progress_at: "2026-04-15T16:08:00Z",
      display_phase: "Recent progress",
      runtime_facts: makeRuntimeFacts({
        control_path: "unmanaged",
        process_state: "closed",
        activity: {
          last_transcript_at: "2026-04-15T16:08:00Z",
          last_runtime_signal_at: null,
          last_progress_at: "2026-04-15T16:08:00Z",
        },
        lifecycle: {
          state: "unknown",
          reason: null,
          observed_at: null,
        },
      }),
      origin_label: "cinder",
      home_label: null,
      capabilities: makeCapabilities(),
      control: null,
    },
    {
      head_origin_label: "cinder",
      started_origin_label: "cinder",
    },
  );

  const closedCodex = makeTimelineCard(
    {
      id: "closed-codex",
      thread_root_session_id: "thread-closed-codex",
      thread_head_session_id: "thread-closed-codex",
      provider: "codex",
      project: "zerg",
      git_branch: "main",
      started_at: "2026-04-15T13:30:00Z",
      ended_at: "2026-04-15T15:20:00Z",
      last_activity_at: "2026-04-15T15:20:00Z",
      timeline_anchor_at: "2026-04-15T15:20:00Z",
      user_messages: 6,
      assistant_messages: 6,
      tool_calls: 22,
      summary_title: "Closed Historical Session",
      summary:
        "Finalized archive import cleanup and closed the terminal process after verifying session history, summaries, and tool counts were durable.",
      status: "completed",
      presence_state: null,
      presence_updated_at: null,
      last_live_at: null,
      runtime_source: null,
      confidence: null,
      display_phase: null,
      runtime_facts: makeRuntimeFacts({
        control_path: "unmanaged",
        activity: {
          last_transcript_at: "2026-04-15T15:20:00Z",
          last_runtime_signal_at: null,
          last_progress_at: null,
        },
        lifecycle: {
          state: "closed",
          reason: "session_ended",
          observed_at: "2026-04-15T15:20:00Z",
        },
      }),
      origin_label: "cinder",
      home_label: null,
      capabilities: makeCapabilities(),
      control: null,
    },
    {
      head_origin_label: "cinder",
      started_origin_label: "cinder",
    },
  );

  const continuationDetail = makeSession({
    id: "continuation-detail",
    provider: "codex",
    project: "longhouse-mobile",
    git_branch: "feature/mobile-card-alignment-pass-with-very-long-branch-name",
    started_at: "2026-04-15T14:40:00Z",
    last_activity_at: "2026-04-15T15:54:00Z",
    timeline_anchor_at: "2026-04-15T15:54:00Z",
    user_messages: 9,
    assistant_messages: 9,
    tool_calls: 31,
    summary_title: "Mobile Timeline Card Structure Pass",
    summary:
      "Pulled identity, execution, and runtime affordances apart so the card can keep a stable rhythm under narrow widths before adding any visual polish.",
    status: "completed",
    thread_root_session_id: "thread-mobile-layout",
    thread_head_session_id: "continuation-head",
    thread_continuation_count: 3,
    origin_label: "MacBook Pro",
    home_label: "On this Mac",
    capabilities: makeCapabilities(),
  });
  const continuationHead = makeSession({
    id: "continuation-head",
    provider: "codex",
    project: "longhouse-mobile",
    git_branch: "feature/mobile-card-alignment-pass-with-very-long-branch-name",
    started_at: "2026-04-15T15:40:00Z",
    last_activity_at: "2026-04-15T16:04:00Z",
    timeline_anchor_at: "2026-04-15T16:04:00Z",
    user_messages: 14,
    assistant_messages: 14,
    tool_calls: 38,
    summary_title: "Current Writable Head",
    summary:
      "This is the newest writable continuation, left here to stress Head and Started badge wrapping with a longer branch name and a reattach capability pill.",
    transcript_preview: {
      event_id: 1002,
      text:
        "I have the mobile card fixture wired now; the provisional preview should stay clipped, readable, and distinct from the durable summary while the turn is still streaming.",
      event_origin: "live_provisional",
      timestamp: "2026-04-15T16:11:29Z",
      is_provisional: true,
      is_complete: false,
      content_cursor: "codex_bridge_live:continuation-head:fixture-thread:fixture-turn:18",
      is_stale: false,
      stale_reason: null,
    },
    status: "working",
    presence_state: "needs_user",
    presence_updated_at: "2026-04-15T16:04:00Z",
    last_live_at: "2026-04-15T16:04:00Z",
    runtime_source: "managed_local_transport",
    confidence: "live",
    display_phase: "Idle",
    runtime_facts: makeRuntimeFacts({
      control_path: "managed",
      host: {
        state: "unknown",
        last_seen_at: null,
        source: null,
      },
      activity: {
        last_transcript_at: null,
        last_runtime_signal_at: null,
        last_progress_at: null,
      },
      lifecycle: {
        state: "unknown",
        reason: null,
        observed_at: null,
      },
    }),
    thread_root_session_id: "thread-mobile-layout",
    thread_head_session_id: "continuation-head",
    thread_continuation_count: 3,
    origin_label: "Cloud",
    home_label: "Moved to cloud",
    capabilities: makeCapabilities({
      live_control_available: false,
      host_reattach_available: true,
      reply_to_live_session_available: false,
    }),
    control: {
      managed_transport: "codex_app_server",
      source_runner_id: null,
      source_runner_name: null,
      attach_command: "longhouse codex --attach continuation-head",
    },
  });
  const continuationRoot = makeSession({
    id: "continuation-root",
    provider: "codex",
    project: "longhouse-mobile",
    git_branch: "feature/mobile-card-alignment-pass-with-very-long-branch-name",
    started_at: "2026-04-15T14:20:00Z",
    last_activity_at: "2026-04-15T14:50:00Z",
    timeline_anchor_at: "2026-04-15T14:50:00Z",
    user_messages: 3,
    assistant_messages: 3,
    tool_calls: 8,
    summary_title: "Root pass",
    summary: "Original structure exploration.",
    status: "completed",
    thread_root_session_id: "thread-mobile-layout",
    thread_head_session_id: "continuation-head",
    thread_continuation_count: 3,
    origin_label: "This machine",
    home_label: "On this Mac",
    capabilities: makeCapabilities(),
  });
  const continuationCard = makeTimelineCard(
    {
      ...continuationDetail,
    },
    {
      thread_id: "thread-mobile-layout",
      timeline_anchor_at: "2026-04-15T16:04:00Z",
      detail: continuationDetail,
      head: continuationHead,
      root: continuationRoot,
      continuation_count: 3,
      started_origin_label: "This machine",
      head_origin_label: "Cloud",
    },
  );

  const blockedGemini = makeTimelineCard(
    {
      id: "blocked-gemini",
      thread_root_session_id: "thread-blocked-gemini",
      thread_head_session_id: "thread-blocked-gemini",
      provider: "gemini",
      project: "photo-restore-lab",
      git_branch: "fix/approval-gate-and-pipeline-resume",
      started_at: "2026-04-15T14:55:00Z",
      last_activity_at: "2026-04-15T15:58:00Z",
      timeline_anchor_at: "2026-04-15T15:58:00Z",
      user_messages: 7,
      assistant_messages: 7,
      tool_calls: 16,
      summary_title: "Approval Gate Handling for Asset Pipeline",
      summary:
        "Agent is blocked waiting on a command approval path, which is useful for stressing the longest runtime phase labels in a narrow card.",
      status: "working",
      presence_state: "blocked",
      presence_tool: "write_stdin",
      active_tool: "write_stdin",
      presence_updated_at: "2026-04-15T15:58:00Z",
      last_live_at: "2026-04-15T15:58:00Z",
      runtime_source: "managed_local_transport",
      confidence: "live",
      display_phase: "Blocked on write_stdin",
      runtime_facts: makeRuntimeFacts({
        control_path: "managed",
        host: {
          state: "online",
          last_seen_at: "2026-04-15T15:58:00Z",
          source: "machine_heartbeat",
        },
        phase: {
          kind: "blocked",
          tool: "write_stdin",
          source: "managed_local_transport",
          observed_at: "2026-04-15T15:58:00Z",
          expires_at: "2026-04-15T16:13:00Z",
        },
        activity: {
          last_transcript_at: "2026-04-15T15:58:00Z",
          last_runtime_signal_at: "2026-04-15T15:58:00Z",
          last_progress_at: null,
        },
        lifecycle: {
          state: "open",
          reason: "phase_observed",
          observed_at: "2026-04-15T15:58:00Z",
        },
      }),
      origin_label: "studio",
      home_label: "On this Mac",
      capabilities: makeCapabilities({
        live_control_available: true,
        host_reattach_available: true,
        reply_to_live_session_available: true,
      }),
      control: {
        managed_transport: "codex_app_server",
        source_runner_id: null,
        source_runner_name: null,
        attach_command: "longhouse gemini --attach blocked-gemini",
      },
    },
    {
      head_origin_label: "studio",
      started_origin_label: "studio",
    },
  );

  const sessions = [
    liveCodex,
    closedCodex,
    idleClaude,
    unmanagedCodex,
    unmanagedClaude,
    continuationCard,
    blockedGemini,
  ];

  return {
    sessions: {
      sessions,
      total: sessions.length,
      has_real_sessions: true,
    },
    filters: {
      projects: ["zerg", "demo-vpn", "project", "longhouse-mobile", "photo-restore-lab"],
      providers: ["claude", "codex", "gemini"],
      machines: ["cinder", "studio", "This machine", "Cloud"],
    },
    runners: {
      runners: [],
    },
  };
}
