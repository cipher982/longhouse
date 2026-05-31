export const SESSION_DETAIL_STRESS_SESSION_ID = "session-detail-stress";
export const SESSION_DETAIL_STRESS_NOW = "2026-04-15T16:12:00Z";

const ROOT_SESSION_ID = "session-detail-root";

type JsonObject = Record<string, unknown>;

type AgentSession = {
  [key: string]: unknown;
  id: string;
  provider: string;
  project: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string | null;
  thread_root_session_id: string;
  thread_head_session_id: string;
  user_messages: number;
  assistant_messages: number;
  tool_calls: number;
  summary_title: string | null;
  summary: string | null;
  control?: JsonObject | null;
  capabilities?: JsonObject | null;
  loop_mode: string;
};

type AgentEvent = {
  id: number;
  role: string;
  content_text: string | null;
  tool_name: string | null;
  tool_input_json: JsonObject | null;
  tool_output_text: string | null;
  tool_call_id: string | null;
  timestamp: string;
  in_active_context?: boolean;
  branch_id?: number | null;
  is_head_branch?: boolean;
};

type AgentSessionProjectionItem = {
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
};

type AgentSessionProjectionResponse = {
  root_session_id: string;
  focus_session_id: string;
  head_session_id: string;
  path_session_ids: string[];
  items: AgentSessionProjectionItem[];
  total: number;
  page_offset?: number;
  branch_mode?: "head" | "all";
  abandoned_events?: number;
};

type AgentSessionThreadResponse = {
  root_session_id: string;
  head_session_id: string;
  sessions: AgentSession[];
};

type AgentSessionWorkspaceResponse = {
  session: AgentSession;
  thread: AgentSessionThreadResponse;
  projection: AgentSessionProjectionResponse;
};

type AgentSessionTurnsListResponse = {
  turns: JsonObject[];
  total: number;
};

function makeSession(overrides: Partial<AgentSession> = {}): AgentSession {
  const now = SESSION_DETAIL_STRESS_NOW;
  return {
    id: "session-detail-base",
    provider: "codex",
    project: "zerg",
    device_id: "device-cinder",
    environment: "development",
    cwd: "/Users/example/git/zerg",
    git_repo: "https://github.com/cipher982/longhouse.git",
    git_branch: "main",
    started_at: "2026-04-15T15:00:00Z",
    ended_at: null,
    last_activity_at: now,
    timeline_anchor_at: now,
    runtime_phase: null,
    phase_started_at: null,
    last_progress_at: now,
    runtime_source: null,
    terminal_state: null,
    runtime_version: 1,
    status: "working",
    presence_state: null,
    presence_tool: null,
    presence_updated_at: null,
    last_live_at: null,
    display_phase: null,
    active_tool: null,
    confidence: null,
    runtime_display: null,
    runtime_facts: null,
    timeline_card: null,
    user_messages: 0,
    assistant_messages: 0,
    tool_calls: 0,
    summary: null,
    summary_title: "Session",
    first_user_message: null,
    match_event_id: null,
    match_snippet: null,
    match_role: null,
    match_score: null,
    thread_root_session_id: ROOT_SESSION_ID,
    thread_head_session_id: SESSION_DETAIL_STRESS_SESSION_ID,
    thread_continuation_count: 1,
    continued_from_session_id: null,
    continuation_kind: null,
    origin_label: "cinder",
    home_label: "On this Mac",
    branched_from_event_id: null,
    is_writable_head: true,
    control: null,
    capabilities: {
      live_control_available: false,
      host_reattach_available: false,
      reply_to_live_session_available: false,
    },
    loop_mode: "assist",
    user_state: undefined,
    ...overrides,
  };
}

function makeEvent(
  id: number,
  role: string,
  timestamp: string,
  overrides: Partial<AgentEvent> = {},
): AgentEvent {
  return {
    id,
    role,
    content_text: null,
    tool_name: null,
    tool_input_json: null,
    tool_output_text: null,
    tool_call_id: null,
    timestamp,
    in_active_context: true,
    branch_id: null,
    is_head_branch: true,
    ...overrides,
  };
}

function projectionEvent(event: AgentEvent, sessionId: string): AgentSessionProjectionItem {
  return {
    kind: "event",
    session_id: sessionId,
    timestamp: event.timestamp,
    event,
  };
}

function toolOutput(exitCode: number, wallTime: string, output: string): string {
  return [
    "Chunk ID: fixture",
    `Wall time: ${wallTime} seconds`,
    `Process exited with code ${exitCode}`,
    "Original token count: 412",
    "Output:",
    output,
  ].join("\n");
}

export function buildSessionDetailStressFixture(): {
  session: AgentSession;
  thread: AgentSessionThreadResponse;
  projection: AgentSessionProjectionResponse;
  workspace: AgentSessionWorkspaceResponse;
  turns: AgentSessionTurnsListResponse;
} {
  const rootSession = makeSession({
    id: ROOT_SESSION_ID,
    started_at: "2026-04-15T14:42:00Z",
    ended_at: "2026-04-15T15:14:30Z",
    last_activity_at: "2026-04-15T15:14:30Z",
    timeline_anchor_at: "2026-04-15T15:14:30Z",
    status: "completed",
    presence_state: null,
    presence_tool: null,
    presence_updated_at: null,
    last_live_at: null,
    display_phase: null,
    runtime_source: null,
    confidence: null,
    runtime_display: {
      truth_tier: "none",
      signal_tier: "none",
      state: null,
      tone: "inactive",
      headline: "Closed",
      detail: null,
      phase_label: "Closed",
      compact_tool_label: null,
      is_live: false,
      is_executing: false,
      needs_attention: false,
      is_idle: false,
      is_managed_local_truth: false,
      has_signal: false,
      control_path: "managed",
      activity_recency: "stale",
      lifecycle: "closed",
      host_state: "online",
      terminal_reason: "provider_signal",
    },
    runtime_facts: {
      control_path: "managed",
      host: {
        state: "online",
        last_seen_at: "2026-04-15T15:14:30Z",
        source: "machine_heartbeat",
      },
      process: {
        status: "not_observed",
        pid: null,
        process_start_time: null,
        observed_at: "2026-04-15T15:14:30Z",
        last_seen_at: null,
        source_mtime: null,
        source_path: null,
        reason: "provider_signal",
        source: "runtime_event",
      },
      phase: {
        kind: null,
        tool: null,
        source: null,
        observed_at: null,
        expires_at: null,
      },
      activity: {
        last_transcript_at: "2026-04-15T15:14:30Z",
        last_runtime_signal_at: "2026-04-15T15:14:30Z",
        last_progress_at: null,
      },
      lifecycle: {
        state: "closed",
        reason: "provider_signal",
        observed_at: "2026-04-15T15:14:30Z",
      },
    },
    user_messages: 4,
    assistant_messages: 5,
    tool_calls: 7,
    summary_title: "Session Page Density Direction",
    summary:
      "Explored a denser session layout and identified the need for a deterministic session-detail QA fixture before doing more visual iteration.",
    first_user_message: "Let's workshop the session page layout.",
    thread_root_session_id: ROOT_SESSION_ID,
    thread_head_session_id: SESSION_DETAIL_STRESS_SESSION_ID,
    thread_continuation_count: 2,
    is_writable_head: false,
    control: null,
    capabilities: {
      live_control_available: false,
      host_reattach_available: false,
      reply_to_live_session_available: false,
      display_label: "Read only",
      display_tone: "neutral",
    },
    loop_mode: "assist",
  });

  const session = makeSession({
    id: SESSION_DETAIL_STRESS_SESSION_ID,
    provider: "codex",
    project: "zerg",
    environment: "development",
    cwd: "/Users/example/git/zerg",
    git_repo: "https://github.com/cipher982/longhouse.git",
    git_branch: "ui/session-workbench-density-prototype",
    started_at: "2026-04-15T15:15:00Z",
    ended_at: null,
    last_activity_at: "2026-04-15T16:11:35Z",
    timeline_anchor_at: "2026-04-15T16:11:35Z",
    status: "working",
    presence_state: "running",
    presence_tool: "exec_command",
    presence_updated_at: "2026-04-15T16:11:35Z",
    last_live_at: "2026-04-15T16:11:35Z",
    display_phase: "Running Shell",
    active_tool: "exec_command",
    runtime_source: "managed_local_transport",
    confidence: "live",
    runtime_display: {
      truth_tier: "managed-local",
      signal_tier: "phase_signal",
      state: "running",
      tone: "running",
      headline: "Running Shell",
      detail: "Running exec_command",
      phase_label: "Running Shell",
      compact_tool_label: "shell",
      is_live: true,
      is_executing: true,
      needs_attention: false,
      is_idle: false,
      is_managed_local_truth: true,
      has_signal: true,
      control_path: "managed",
      activity_recency: "live",
      lifecycle: "open",
      host_state: "online",
      terminal_reason: null,
    },
    runtime_facts: {
      control_path: "managed",
      host: {
        state: "online",
        last_seen_at: "2026-04-15T16:11:35Z",
        source: "machine_heartbeat",
      },
      process: {
        status: "observed",
        pid: 74465,
        process_start_time: "2026-04-15T15:15:00Z",
        observed_at: "2026-04-15T16:11:35Z",
        last_seen_at: "2026-04-15T16:11:35Z",
        source_mtime: "2026-04-15T16:11:35Z",
        source_path: "/Users/example/.codex/sessions/session-detail-stress.jsonl",
        reason: null,
        source: "managed_local_transport",
      },
      phase: {
        kind: "running",
        tool: "exec_command",
        source: "managed_local_transport",
        observed_at: "2026-04-15T16:11:35Z",
        expires_at: "2026-04-15T16:26:35Z",
      },
      activity: {
        last_transcript_at: "2026-04-15T16:11:35Z",
        last_runtime_signal_at: "2026-04-15T16:11:35Z",
        last_progress_at: "2026-04-15T16:11:35Z",
      },
      lifecycle: {
        state: "open",
        reason: "phase_observed",
        observed_at: "2026-04-15T16:11:35Z",
      },
    },
    user_messages: 7,
    assistant_messages: 8,
    tool_calls: 14,
    summary_title: "UI Workbench Fixture for Session Detail",
    summary:
      "Fixture-backed session detail capture with branch seam, dense transcript rows, completed tools, a failing command, and one currently running shell command.",
    first_user_message:
      "The session page is too dull and wastes too much space. Let's workshop a denser layout.",
    thread_root_session_id: ROOT_SESSION_ID,
    thread_head_session_id: SESSION_DETAIL_STRESS_SESSION_ID,
    thread_continuation_count: 2,
    continued_from_session_id: ROOT_SESSION_ID,
    continuation_kind: "local",
    origin_label: "cinder",
    home_label: "On this Mac",
    branched_from_event_id: 104,
    is_writable_head: true,
    control: {
      managed_transport: "codex_app_server",
      source_runner_id: 7,
      source_runner_name: "cinder",
      attach_command: `longhouse codex --attach ${SESSION_DETAIL_STRESS_SESSION_ID}`,
    },
    capabilities: {
      live_control_available: true,
      host_reattach_available: true,
      reply_to_live_session_available: true,
      can_queue_next_input: true,
      can_steer_active_turn: true,
      display_label: "Live on cinder",
      display_detail: "Managed local Codex control path",
      display_tone: "success",
    },
    loop_mode: "assist",
  });

  const rootEvents: AgentEvent[] = [
    makeEvent(101, "user", "2026-04-15T14:43:00Z", {
      content_text:
        "The transcript page feels like a document viewer. What layouts could make this feel more like a power-user command surface?",
    }),
    makeEvent(102, "assistant", "2026-04-15T14:44:20Z", {
      content_text:
        "The strongest direction is a dense workbench: compact turn blocks, fixed runtime strip, keyboardable filters, and a mode that jumps between user prompts.",
    }),
    makeEvent(103, "assistant", "2026-04-15T14:45:10Z", {
      tool_name: "exec_command",
      tool_input_json: { cmd: "rg -n \"session-workspace|timeline-pane\" web/src" },
      tool_call_id: "root-tool-1",
    }),
    makeEvent(104, "tool", "2026-04-15T14:45:11Z", {
      tool_name: "exec_command",
      tool_output_text: toolOutput(
        0,
        "1.1",
        "web/src/pages/SessionDetailPage.tsx:24:import { SessionRuntimeStrip }\nweb/src/components/session-workspace/TimelinePane.tsx:587:export function TimelinePane",
      ),
      tool_call_id: "root-tool-1",
    }),
  ];

  const headEvents: AgentEvent[] = [
    makeEvent(201, "user", "2026-04-15T15:16:00Z", {
      content_text:
        "Yes, I agree on the path. But we need fast mockup iteration so we are not editing, deploying, and manually reviewing every idea.",
    }),
    makeEvent(202, "assistant", "2026-04-15T15:16:40Z", {
      content_text:
        "I am going to add a session-detail capture fixture first. That gives us a stable layout lab before we start changing the real page.",
    }),
    makeEvent(203, "assistant", "2026-04-15T15:17:02Z", {
      tool_name: "exec_command",
      tool_input_json: { cmd: "git status --short --branch" },
      tool_call_id: "head-tool-1",
    }),
    makeEvent(204, "tool", "2026-04-15T15:17:03Z", {
      tool_name: "exec_command",
      tool_output_text: toolOutput(0, "0.8", "## ui-qa-cleanup...origin/main [ahead 1]"),
      tool_call_id: "head-tool-1",
    }),
    makeEvent(205, "assistant", "2026-04-15T15:17:34Z", {
      tool_name: "exec_command",
      tool_input_json: {
        cmd: "sed -n '1,260p' scripts/ui-capture.ts",
      },
      tool_call_id: "head-tool-2",
    }),
    makeEvent(206, "tool", "2026-04-15T15:17:35Z", {
      tool_name: "exec_command",
      tool_output_text: toolOutput(
        0,
        "1.2",
        "const PAGE_DEFINITIONS = {\n  timeline: { path: \"/timeline\" },\n  machines: { path: \"/runners\" },\n};",
      ),
      tool_call_id: "head-tool-2",
    }),
    makeEvent(207, "assistant", "2026-04-15T15:18:15Z", {
      tool_name: "apply_patch",
      tool_input_json: {
        file_path: "scripts/ui-capture.ts",
        description: "Add session-detail page and mock scene routing.",
      },
      tool_call_id: "head-tool-3",
    }),
    makeEvent(208, "tool", "2026-04-15T15:18:17Z", {
      tool_name: "apply_patch",
      tool_output_text: "Success. Updated the following files:\nM scripts/ui-capture.ts",
      tool_call_id: "head-tool-3",
    }),
    makeEvent(209, "assistant", "2026-04-15T15:19:00Z", {
      content_text:
        "The first cut is wired. I am checking it with the capture harness now, then I will look at the screenshot for density and obvious layout waste.",
    }),
    makeEvent(210, "assistant", "2026-04-15T15:19:35Z", {
      tool_name: "exec_command",
      tool_input_json: {
        cmd: "make ui-capture PAGE=session-detail SCENE=session-detail-stress VIEWPORT=desktop NO_TRACE=1",
      },
      tool_call_id: "head-tool-4",
    }),
    makeEvent(211, "tool", "2026-04-15T15:20:05Z", {
      tool_name: "exec_command",
      tool_output_text: toolOutput(
        1,
        "30.0",
        "Error: page.waitForSelector: Timeout 5000ms exceeded.\nThe workspace did not mark data-screenshot-ready=true.",
      ),
      tool_call_id: "head-tool-4",
    }),
    makeEvent(212, "assistant", "2026-04-15T15:21:00Z", {
      content_text:
        "The failure is useful: the session page readiness flag only flips after both workspace and projection finish. I need to mock the projection and turn endpoints too.",
    }),
    makeEvent(213, "user", "2026-04-15T15:22:00Z", {
      content_text:
        "Exactly. Make this useful for catching the dumb stuff before I load production and notice padding, dull contrast, or labels that do not make sense.",
    }),
    makeEvent(214, "assistant", "2026-04-15T15:22:34Z", {
      content_text:
        "Agreed. This fixture should stress the parts you actually judge by eye: who said what, what is running now, how much work happened, and whether managed control is obvious.",
    }),
    makeEvent(215, "assistant", "2026-04-15T16:11:35Z", {
      tool_name: "exec_command",
      tool_input_json: {
        cmd: "make ui-capture PAGE=session-detail SCENE=session-detail-stress VIEWPORT=mobile NO_TRACE=1",
      },
      tool_call_id: "head-tool-5",
    }),
  ];

  const items: AgentSessionProjectionItem[] = [
    ...rootEvents.map((event) => projectionEvent(event, ROOT_SESSION_ID)),
    {
      kind: "seam",
      session_id: SESSION_DETAIL_STRESS_SESSION_ID,
      timestamp: "2026-04-15T15:15:00Z",
      event: null,
      continued_from_session_id: ROOT_SESSION_ID,
      continuation_kind: "local",
      origin_label: "cinder",
      parent_origin_label: "earlier branch",
      parent_continuation_kind: null,
      branched_from_event_id: 104,
    },
    ...headEvents.map((event) => projectionEvent(event, SESSION_DETAIL_STRESS_SESSION_ID)),
  ];

  const projection: AgentSessionProjectionResponse = {
    root_session_id: ROOT_SESSION_ID,
    focus_session_id: SESSION_DETAIL_STRESS_SESSION_ID,
    head_session_id: SESSION_DETAIL_STRESS_SESSION_ID,
    path_session_ids: [ROOT_SESSION_ID, SESSION_DETAIL_STRESS_SESSION_ID],
    items,
    total: 1259,
    page_offset: 1239,
    branch_mode: "head",
    abandoned_events: 18,
  };

  const thread: AgentSessionThreadResponse = {
    root_session_id: ROOT_SESSION_ID,
    head_session_id: SESSION_DETAIL_STRESS_SESSION_ID,
    sessions: [rootSession, session],
  };

  const workspace: AgentSessionWorkspaceResponse = {
    session,
    thread,
    projection,
  };

  const turns: AgentSessionTurnsListResponse = {
    total: 2,
    turns: [
      {
        id: 77,
        session_id: SESSION_DETAIL_STRESS_SESSION_ID,
        request_id: "turn-session-detail-mobile-capture",
        state: "active",
        terminal_phase: null,
        error_code: null,
        user_event_id: 213,
        durable_assistant_event_id: null,
        baseline_event_id: 214,
        baseline_observation_cursor: null,
        user_submitted_at: "2026-04-15T15:22:00Z",
        send_accepted_at: "2026-04-15T15:22:01Z",
        active_phase_observed_at: "2026-04-15T16:11:35Z",
        terminal_at: null,
        durable_at: null,
        created_at: "2026-04-15T15:22:00Z",
        updated_at: "2026-04-15T16:11:35Z",
      },
      {
        id: 76,
        session_id: SESSION_DETAIL_STRESS_SESSION_ID,
        request_id: "turn-session-detail-desktop-capture",
        state: "failed",
        terminal_phase: "failed",
        error_code: "capture_timeout",
        user_event_id: 201,
        durable_assistant_event_id: 212,
        baseline_event_id: 200,
        baseline_observation_cursor: null,
        user_submitted_at: "2026-04-15T15:16:00Z",
        send_accepted_at: "2026-04-15T15:16:01Z",
        active_phase_observed_at: "2026-04-15T15:16:40Z",
        terminal_at: "2026-04-15T15:21:00Z",
        durable_at: "2026-04-15T15:21:00Z",
        created_at: "2026-04-15T15:16:00Z",
        updated_at: "2026-04-15T15:21:00Z",
      },
    ],
  };

  return { session, thread, projection, workspace, turns };
}
