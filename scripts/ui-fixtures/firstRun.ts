/**
 * A brand-new Runtime Host after its first machine connected: one Machine
 * Agent online and five imported sessions (three Claude Code, two Codex), no
 * live evidence for any of them, and no text model configured.
 *
 * The session shape is what a real host serves for imported Shadow history
 * (captured 2026-10-01 from a scratch Runtime Host with synthetic transcripts):
 * no run, activity `unknown` with no `raw_kind`, no live observation, titles
 * that are the first prompt cut to six words (`title_source: "prompt"`).
 */
import { makeTimelineCard } from "./timelineCardStress";

/** The fixture clock. ui-capture pins `Date.now` to this for fixture scenes. */
export const FIRST_RUN_NOW = Date.parse("2026-04-15T16:12:00Z");

const at = (hoursAgo: number): string => new Date(FIRST_RUN_NOW - hoursAgo * 3_600_000).toISOString();

type CardOverrides = NonNullable<Parameters<typeof makeTimelineCard>[0]>;

function importedState(headline: { key: string; label: string; tone: string }): CardOverrides["session_state"] {
  const unavailable = (reason: string) => ({ state: "unavailable", reason });
  return {
    state_contract_version: 4,
    presentation_policy_version: 4,
    mode: "shadow",
    disposition: { state: "open", closed_at: null, close_reason: null },
    launch: null,
    run: null,
    activity: { state: "unknown", raw_kind: null, tool: null, source: null, observed_at: null, valid_until: null },
    control: {
      ownership: "unowned",
      connection: "not_applicable",
      actions: {
        start_turn: unavailable("not_console"),
        send_input: unavailable("observe_only"),
        interrupt: unavailable("observe_only"),
        terminate: unavailable("observe_only"),
        reattach: unavailable("not_helm"),
        resume: unavailable("not_helm"),
        branch: unavailable("not_helm"),
      },
    },
    pending_interaction: null,
    transcript: { convergence: "current", searchable: true, live_observation: false },
    host: { state: "online", observed_at: at(0.01) },
    working_set: "history",
    presentation: {
      primary: { ...headline, observed_at: null },
      access: { key: "search_only", label: "Search only", tone: "search", observed_at: null },
      transcript: null,
    },
  } as unknown as CardOverrides["session_state"];
}

interface ImportedSpec {
  id: string;
  provider: "claude" | "codex";
  project: string;
  branch: string;
  hoursAgo: number;
  prompt: string;
}

const IMPORTED: ImportedSpec[] = [
  {
    id: "imported-public-api",
    provider: "claude",
    project: "public-api",
    branch: "feature/rate-limit",
    hoursAgo: 26,
    prompt: "Add rate limiting to the public API and cover it with tests",
  },
  {
    id: "imported-web-app-todos",
    provider: "codex",
    project: "web-app",
    branch: "main",
    hoursAgo: 31,
    prompt: "Summarise the open TODOs in this repo and group them by owner",
  },
  {
    id: "imported-web-app-hooks",
    provider: "codex",
    project: "web-app",
    branch: "main",
    hoursAgo: 53,
    prompt: "Migrate the settings page from class components to hooks",
  },
  {
    id: "imported-data-pipeline",
    provider: "claude",
    project: "data-pipeline",
    branch: "fix/export-timeout",
    hoursAgo: 73,
    prompt: "Why does the nightly export job fail with a timeout on the orders table? Find the slow query and fix it.",
  },
  {
    id: "imported-billing",
    provider: "claude",
    project: "billing-service",
    branch: "main",
    hoursAgo: 123,
    prompt: "Write a Dockerfile and a docker-compose file for the billing service, then document how to run it locally",
  },
];

/** First six words plus an ellipsis: the headline a host with no title model serves. */
function promptHeadline(prompt: string): string {
  const words = prompt.split(/\s+/);
  return words.length > 6 ? `${words.slice(0, 6).join(" ")}…` : prompt;
}

export function buildFirstRunMachineFixture() {
  const headline = { key: "imported", label: "Imported", tone: "inactive" };
  const cards = IMPORTED.map((spec) => {
    const title = promptHeadline(spec.prompt);
    const state = importedState(headline);
    return makeTimelineCard({
      id: spec.id,
      thread_root_session_id: spec.id,
      thread_head_session_id: spec.id,
      provider: spec.provider,
      project: spec.project,
      cwd: `/Users/alex/work/${spec.project}`,
      git_repo: null,
      git_branch: spec.branch,
      device_id: "alex-macbook",
      environment: null,
      started_at: at(spec.hoursAgo + 0.1),
      last_activity_at: at(spec.hoursAgo),
      timeline_anchor_at: at(spec.hoursAgo),
      user_messages: 1,
      assistant_messages: 2,
      tool_calls: 0,
      summary: null,
      summary_title: title,
      anchor_title: null,
      timeline_title: title,
      title_source: "prompt",
      first_user_message: spec.prompt,
      origin_label: "local",
      home_label: null,
      is_writable_head: false,
      session_state: state,
      runtime_display: {
        truth_tier: "none",
        signal_tier: "none",
        state: null,
        tone: "inactive",
        headline: "Inactive",
        detail: null,
        phase_label: "Inactive",
        compact_tool_label: null,
        is_live: false,
        is_executing: false,
        needs_attention: false,
        is_idle: false,
        is_stalled: false,
        is_managed_local_truth: false,
        has_signal: false,
        control_path: "unmanaged",
        activity_recency: "none",
        lifecycle: "open",
        host_state: "online",
        terminal_reason: null,
        pause_request: null,
      },
      timeline_card: {
        ownership: { label: "Unmanaged", tone: "neutral" },
        status: {
          label: headline.label,
          tone: headline.tone,
          seen_at: null,
          seen_at_prefix: "Updated",
        },
        border_tone: headline.tone,
      },
    });
  });

  return {
    sessions: { sessions: cards, total: cards.length, has_real_sessions: true, history_imports: [] },
    filters: {
      projects: [...new Set(IMPORTED.map((spec) => spec.project))],
      providers: ["claude", "codex"],
      machines: ["alex-macbook"],
    },
    machines: {
      machines: [
        {
          device_id: "alex-macbook",
          machine_name: "alex-macbook",
          online: true,
          control_channel_status: "connected",
          supports: ["claude.turn_start", "codex.turn_start"],
          control_operations_by_provider: { claude: ["turn_start"], codex: ["turn_start"] },
          last_seen_at: at(0.002),
          connected_since: at(0.07),
          engine_build: "0.1.62",
          provider_readiness: { claude: { state: "ready" }, codex: { state: "ready" } },
          launch: {
            blocked_by: null,
            providers: [{ provider: "claude" }, { provider: "codex" }],
            default_provider: "claude",
            unavailable_providers: [],
          },
        },
      ],
    },
    runners: { runners: [] },
  };
}
