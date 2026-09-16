/**
 * Curated data for the landing page's product showcase (Timeline / Search /
 * Session Detail tabs). Rendered through the real web app with mocked API
 * responses by `make landing-screenshots`, so the images always match the
 * current product instead of whatever the UI looked like on capture day.
 *
 * The story continues the landing hero: several providers on several
 * machines, one of them a Helm session you can steer, and a history that
 * search can reach back into.
 */
import { buildTimelineCardStressFixture, makeTimelineCard } from "./timelineCardStress";
import {
  SESSION_DETAIL_STRESS_NOW,
  SESSION_DETAIL_STRESS_SESSION_ID,
  buildSessionDetailStressFixture,
  makeEvent,
  projectionEvent,
} from "./sessionDetailStress";

/** Same fixture clock as the stress scenes (ui-capture freezes Date.now to it). */
const NOW = "2026-04-15T16:12:00Z";
const minutesAgo = (m: number) => new Date(Date.parse(NOW) - m * 60_000).toISOString();
const daysAgo = (d: number, hour = 15) => {
  const t = new Date(Date.parse(NOW) - d * 86_400_000);
  t.setUTCHours(hour, 20, 0, 0);
  return t.toISOString();
};

type CardInput = Parameters<typeof makeTimelineCard>[0];

interface LandingCard {
  id: string;
  provider: string;
  project: string;
  machine: string;
  branch: string;
  title: string;
  prompt: string;
  summary: string;
  lastActivity: string;
  /** helm = launched through Longhouse (steerable); shadow = observed. */
  mode: "helm" | "shadow";
  activity?: "running" | "thinking" | "idle";
  tool?: string;
  closed?: boolean;
  counts: [user: number, assistant: number, tools: number];
}

function card(c: LandingCard) {
  const live = !c.closed && c.activity !== undefined;
  const overrides: CardInput = {
    id: c.id,
    thread_root_session_id: `thread-${c.id}`,
    thread_head_session_id: `thread-${c.id}`,
    provider: c.provider,
    project: c.project,
    git_branch: c.branch,
    cwd: `/Users/you/code/${c.project}`,
    device_id: `device-${c.machine}`,
    origin_label: c.machine,
    home_label: c.machine,
    started_at: c.lastActivity,
    last_activity_at: c.lastActivity,
    timeline_anchor_at: c.lastActivity,
    summary_title: c.title,
    anchor_title: c.title,
    timeline_title: c.title,
    first_user_message: c.prompt,
    summary: c.summary,
    user_messages: c.counts[0],
    assistant_messages: c.counts[1],
    tool_calls: c.counts[2],
    status: live ? "working" : "completed",
    terminal_state: c.closed ? "user_closed" : null,
    confidence: live ? "live" : null,
    presence_state: live ? c.activity : null,
    presence_tool: c.activity === "running" ? (c.tool ?? null) : null,
    active_tool: c.activity === "running" ? (c.tool ?? null) : null,
    presence_updated_at: live ? c.lastActivity : null,
    last_live_at: live ? c.lastActivity : null,
    capabilities: {
      live_control_available: c.mode === "helm" && !c.closed,
      host_reattach_available: c.mode === "helm",
      reply_to_live_session_available: c.mode === "helm" && !c.closed,
    },
  };
  const built = makeTimelineCard(overrides);
  // The inbox shelves a card as "Live now" from the server's working_set tier.
  const workingSet = c.closed ? "history" : "open";
  for (const session of [built.head, built.detail, built.root]) {
    (session.session_state as { working_set?: string }).working_set = workingSet;
  }
  return built;
}

const CARDS: LandingCard[] = [
  {
    id: "landing-claude-inventory",
    provider: "claude",
    project: "demo-repo",
    machine: "macbook",
    branch: "main",
    title: "Add an empty-shelf test and rerun the suite",
    prompt: "Add a test for an empty shelf, then rerun the tests",
    summary: "Added test_count_items_empty; all four inventory tests pass.",
    lastActivity: minutesAgo(0.2),
    mode: "helm",
    activity: "running",
    tool: "Bash",
    counts: [2, 7, 5],
  },
  {
    id: "landing-codex-webhooks",
    provider: "codex",
    project: "shop-api",
    machine: "devbox",
    branch: "retry-webhooks",
    title: "Retry failed payment webhooks with backoff",
    prompt: "Payment webhooks drop on a 502 from the provider. Retry with exponential backoff and cap it.",
    summary: "Wrapping the webhook sender in a bounded retry; writing the failure-path test next.",
    lastActivity: minutesAgo(1),
    mode: "helm",
    activity: "thinking",
    counts: [3, 11, 14],
  },
  {
    id: "landing-cursor-settings",
    provider: "cursor",
    project: "web-app",
    machine: "studio",
    branch: "settings-forms",
    title: "Migrate the settings page to the new form library",
    prompt: "Move the settings page onto the new form library and keep validation messages identical.",
    summary: "Settings form migrated; three validation messages still use the old helper.",
    lastActivity: minutesAgo(6),
    mode: "shadow",
    activity: "idle",
    counts: [4, 9, 22],
  },
  {
    id: "landing-opencode-backups",
    provider: "opencode",
    project: "homelab",
    machine: "homelab",
    branch: "main",
    title: "Check last night's Postgres backups restored cleanly",
    prompt: "Restore last night's Postgres backup into a scratch container and compare row counts.",
    summary: "Restore verified: row counts match for all 42 tables.",
    lastActivity: minutesAgo(38),
    mode: "shadow",
    closed: true,
    counts: [1, 5, 9],
  },
  {
    id: "landing-claude-flaky-checkout",
    provider: "claude",
    project: "shop-api",
    machine: "macbook",
    branch: "fix-flaky-checkout",
    title: "Fix the flaky checkout test",
    prompt: "test_checkout_applies_coupon fails about one run in ten on CI. Find out why.",
    summary: "The test shared a cached cart across runs; isolated the fixture and the flake is gone.",
    lastActivity: daysAgo(2),
    mode: "helm",
    closed: true,
    counts: [3, 12, 18],
  },
  {
    id: "landing-codex-flaky-login",
    provider: "codex",
    project: "web-app",
    machine: "devbox",
    branch: "main",
    title: "Quarantine the flaky login end-to-end spec",
    prompt: "The login e2e spec is flaky on Safari. Quarantine it and open an issue with the traces.",
    summary: "Marked login.spec as quarantined on WebKit and attached three failing traces.",
    lastActivity: daysAgo(19, 11),
    mode: "shadow",
    closed: true,
    counts: [2, 6, 8],
  },
  {
    id: "landing-cursor-flaky-ci",
    provider: "cursor",
    project: "shop-api",
    machine: "studio",
    branch: "ci-timeouts",
    title: "Why are integration tests flaky only on CI?",
    prompt: "Integration tests are flaky on CI but never locally. What is different?",
    summary: "CI runs tests in parallel against one database; serialized the migration step.",
    lastActivity: daysAgo(34, 17),
    mode: "shadow",
    closed: true,
    counts: [5, 10, 16],
  },
];

export const LANDING_SEARCH_QUERY = "flaky";

/** Timeline, or search results when `query` is set (matched like a person reads them). */
export function buildLandingTimelineFixture(query = ""): ReturnType<typeof buildTimelineCardStressFixture> {
  const needle = query.trim().toLowerCase();
  const chosen = CARDS.filter((c) => {
    if (!needle) return c.lastActivity >= daysAgo(3);
    return [c.title, c.prompt, c.summary].some((text) => text.toLowerCase().includes(needle));
  });
  const sessions = chosen.map(card);
  return {
    sessions: { sessions, total: sessions.length, has_real_sessions: true },
    filters: {
      projects: [...new Set(CARDS.map((c) => c.project))],
      providers: [...new Set(CARDS.map((c) => c.provider))],
      machines: [...new Set(CARDS.map((c) => c.machine))],
    },
    runners: { runners: [] },
  } as ReturnType<typeof buildTimelineCardStressFixture>;
}

/**
 * The Claude session from the hero, opened: its transcript, tool rows, and
 * the live composer, mid-turn on the follow-up sent from the phone.
 */
export function buildLandingSessionFixture(): ReturnType<typeof buildSessionDetailStressFixture> {
  const fixture = buildSessionDetailStressFixture();
  const id = SESSION_DETAIL_STRESS_SESSION_ID;
  const at = (m: number) => new Date(Date.parse(SESSION_DETAIL_STRESS_NOW) - m * 60_000).toISOString();

  const events = [
    makeEvent(1, "user", at(9), {
      content_text: "Fix the off-by-one bug in count_items in inventory.py, then run: python3 test_inventory.py",
    }),
    makeEvent(2, "assistant", at(8.9), { content_text: "Let me look at inventory.py." }),
    makeEvent(3, "assistant", at(8.8), {
      tool_name: "Read",
      tool_input_json: { file_path: "/Users/you/code/demo-repo/inventory.py" },
      tool_call_id: "read-1",
    }),
    makeEvent(4, "tool", at(8.8), {
      tool_name: "Read",
      tool_output_text: "def count_items(shelves):\n    total = 0\n    for shelf in shelves[1:]:\n        total += len(shelf)\n    return total",
      tool_call_id: "read-1",
    }),
    makeEvent(5, "assistant", at(8.5), {
      content_text:
        "Found it: count_items starts the loop at index 1, so the first shelf is never counted. Fixing it.",
    }),
    makeEvent(6, "assistant", at(8.4), {
      tool_name: "Edit",
      tool_input_json: {
        file_path: "/Users/you/code/demo-repo/inventory.py",
        old_string: "for shelf in shelves[1:]:",
        new_string: "for shelf in shelves:",
      },
      tool_call_id: "edit-1",
    }),
    makeEvent(7, "tool", at(8.4), { tool_name: "Edit", tool_output_text: "Updated inventory.py", tool_call_id: "edit-1" }),
    makeEvent(8, "assistant", at(8.2), {
      tool_name: "Bash",
      tool_input_json: { command: "python3 test_inventory.py", description: "Run inventory tests" },
      tool_call_id: "bash-1",
    }),
    makeEvent(9, "tool", at(8.1), { tool_name: "Bash", tool_output_text: "all tests passed", tool_call_id: "bash-1" }),
    makeEvent(10, "assistant", at(8), {
      content_text: "Fixed the off-by-one in count_items: the loop now covers every shelf and all three tests pass.",
    }),
    makeEvent(11, "user", at(1.2), { content_text: "Add a test for an empty shelf, then rerun the tests" }),
    makeEvent(12, "assistant", at(1.1), { content_text: "Adding an empty-shelf case next to the existing count tests." }),
    makeEvent(13, "assistant", at(1), {
      tool_name: "Edit",
      tool_input_json: {
        file_path: "/Users/you/code/demo-repo/test_inventory.py",
        old_string: "def test_busiest_shelf():",
        new_string: "def test_count_items_empty():\n    assert count_items([]) == 0\n\n\ndef test_busiest_shelf():",
      },
      tool_call_id: "edit-2",
    }),
    makeEvent(14, "tool", at(1), { tool_name: "Edit", tool_output_text: "Updated test_inventory.py", tool_call_id: "edit-2" }),
    makeEvent(15, "assistant", at(0.4), {
      tool_name: "Bash",
      tool_input_json: { command: "python3 test_inventory.py", description: "Rerun inventory tests" },
      tool_call_id: "bash-2",
      tool_call_state: "running",
    }),
  ];

  const session = {
    ...fixture.session,
    provider: "claude",
    project: "demo-repo",
    cwd: "/Users/you/code/demo-repo",
    git_branch: "main",
    origin_label: "macbook",
    home_label: "macbook",
    device_id: "device-macbook",
    summary_title: "Add an empty-shelf test and rerun the suite",
    anchor_title: "Add an empty-shelf test and rerun the suite",
    timeline_title: "Add an empty-shelf test and rerun the suite",
    first_user_message: events[0].content_text,
    summary: "Fixed count_items and is adding an empty-shelf test on request from the phone.",
    user_messages: 2,
    assistant_messages: 6,
    tool_calls: 5,
    active_tool: "Bash",
    presence_tool: "Bash",
    display_phase: "Running Bash",
    control: {
      ...(fixture.session.control as Record<string, unknown>),
      source_runner_name: "macbook",
      attach_command: `longhouse claude --attach ${id}`,
    },
    capabilities: {
      ...(fixture.session.capabilities as Record<string, unknown>),
      display_label: "Live on macbook",
      display_detail: "Managed local Claude Code control path",
    },
    session_state: {
      ...(fixture.session.session_state as Record<string, unknown>),
      activity: {
        state: "executing",
        raw_kind: "running",
        tool: "Bash",
        source: "managed_local_transport",
        observed_at: at(0.4),
        valid_until: null,
      },
      run: { lifecycle: "running", started_at: at(9), ended_at: null },
      presentation: {
        primary: { key: "executing", label: "Using Bash", tone: "running", observed_at: at(0.4) },
        access: { key: "live_control", label: "Live control", tone: "live", observed_at: at(0.4) },
        transcript: null,
      },
    },
  } as typeof fixture.session;

  const items = events.map((event) => projectionEvent(event, id));
  const projection = {
    root_session_id: id,
    focus_session_id: id,
    head_session_id: id,
    path_session_ids: [id],
    items,
    total: items.length,
    page_offset: 0,
    branch_mode: "head",
    abandoned_events: 0,
  } as typeof fixture.projection;
  const thread = { root_session_id: id, head_session_id: id, sessions: [session] } as typeof fixture.thread;

  return {
    session,
    thread,
    projection,
    workspace: { session, thread, projection } as typeof fixture.workspace,
    turns: { total: 0, turns: [] } as typeof fixture.turns,
  };
}
