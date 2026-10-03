/**
 * A personal fleet for the Machines page and a machine's page, shaped like the
 * owner's real host on 2026-10-03 (/api/timeline/machines/summary): one busy
 * Mac with nine live sessions, a bench box with Codex signed out, an idle CI
 * box, a server that ships sessions without a live connection, and four
 * machines nobody has heard from in a week or more. Timestamps hang off the
 * shared fixture clock so relative times read the same on every capture.
 */
import { FIRST_RUN_NOW } from "./firstRun";

const DAY_MS = 86_400_000;
const at = (minutesAgo: number): string => new Date(FIRST_RUN_NOW - minutesAgo * 60_000).toISOString();
const days = (count: number): string[] =>
  Array.from({ length: count }, (_, index) => new Date(FIRST_RUN_NOW - (count - 1 - index) * DAY_MS).toISOString().slice(0, 10));

const CALENDAR = days(14);

function daily(counts: Array<Record<string, number>>) {
  return CALENDAR.map((date, index) => {
    const by_provider = counts[index] ?? {};
    return { date, total: Object.values(by_provider).reduce((sum, count) => sum + count, 0), by_provider };
  });
}

const CINDER_DAYS = [
  { omp: 43, cursor: 1, opencode: 1 },
  { omp: 21, cursor: 10 },
  { claude: 3, omp: 21 },
  { claude: 5, omp: 15, cursor: 2 },
  { claude: 5, omp: 9, pi: 1, antigravity: 1 },
  { omp: 11, claude: 4, pi: 1, codex: 1 },
  { claude: 4, omp: 3 },
  { claude: 5, omp: 5 },
  { claude: 8, omp: 11, codex: 1 },
  { omp: 10, claude: 12, opencode: 2 },
  { omp: 10, claude: 2 },
  { omp: 6 },
  { omp: 2 },
  { omp: 9 },
];

const ready = (providers: string[], defaultProvider: string) => ({
  blocked_by: null,
  providers: providers.map((provider) => ({ provider })),
  default_provider: defaultProvider,
  unavailable_providers: [] as Array<{ provider: string; reason: string; remediation: string | null }>,
});

const offline = { blocked_by: "control_down", providers: [], default_provider: null, unavailable_providers: [] };

function machine(overrides: Record<string, unknown>) {
  return {
    supports: [],
    control_operations_by_provider: {},
    engine_build: null,
    connected_since: null,
    provider_readiness: {},
    ...overrides,
  };
}

function live(id: string, title: string, project: string, minutesAgo: number, provider = "omp") {
  return { session_id: id, title, project, provider, last_activity_at: at(minutesAgo), activity_state: "thinking" };
}

function sync(overrides: Record<string, unknown> = {}) {
  return {
    reported_at: at(0.2),
    report_age_seconds: 12,
    stale: false,
    status: "healthy",
    status_summary: "Shipping healthy.",
    engine_version: "0.1.64",
    last_upload_at: at(0.15),
    upload_p95_ms: 1755,
    waiting_uploads: 0,
    failed_uploads: 0,
    history: { state: "current", source_count: 7639, remaining_bytes: 0, remaining_records: 0, acknowledged_records: 46_326_204 },
    ...overrides,
  };
}

const empty = (latest: unknown = null) => ({
  sessions_started: 0,
  daily: daily([]),
  top_projects: [],
  latest_session: latest,
  live_count: 0,
  live_sessions: [],
});

export function buildMachinesFleetFixture() {
  const cinder = machine({
    device_id: "cinder",
    machine_name: "cinder",
    online: true,
    control_channel_status: "connected",
    last_seen_at: at(0.1),
    connected_since: at(12 * 60 + 32),
    engine_build: "307e4143",
    supports: ["claude.turn_start", "codex.turn_start", "omp.turn_start", "cursor.turn_start", "opencode.turn_start", "pi.turn_start"],
    provider_readiness: { claude: { state: "ready" }, codex: { state: "ready" }, omp: { state: "unknown" }, cursor: { state: "unknown" } },
    launch: ready(["antigravity", "claude", "codex", "cursor", "omp", "opencode", "pi"], "codex"),
  });
  const bench = machine({
    device_id: "cube-bench",
    machine_name: "cube-bench",
    online: true,
    control_channel_status: "connected",
    last_seen_at: at(0.3),
    connected_since: at(18 * 60 + 37),
    engine_build: "051ce873",
    supports: ["codex.sign_in"],
    provider_readiness: { claude: { state: "ready" }, codex: { state: "not_authenticated" } },
    launch: {
      ...ready(["claude", "cursor", "omp", "opencode"], "claude"),
      blocked_by: null,
      unavailable_providers: [{ provider: "codex", reason: "not_authenticated", remediation: null }],
    },
  });
  const cube = machine({
    device_id: "cube",
    machine_name: "cube",
    online: true,
    control_channel_status: "connected",
    last_seen_at: at(0.2),
    connected_since: at(18 * 60 + 46),
    engine_build: "97a6670a",
    launch: ready(["claude"], "claude"),
  });
  const clifford = machine({
    device_id: "clifford-sauron",
    machine_name: "clifford-sauron",
    online: false,
    control_channel_status: "disconnected",
    last_seen_at: at(66 * 24 * 60),
    launch: offline,
  });
  const quietMachine = (name: string, daysAgo: number) =>
    machine({
      device_id: name,
      machine_name: name,
      online: false,
      control_channel_status: "disconnected",
      last_seen_at: at(daysAgo * 24 * 60),
      launch: offline,
    });

  const machines = [
    {
      machine: cinder,
      activity: {
        sessions_started: 254,
        daily: daily(CINDER_DAYS),
        top_projects: [
          { project: "zerg", sessions: 78 },
          { project: "zeta", sessions: 57 },
          { project: "davidrose", sessions: 55 },
        ],
        latest_session: live("5d6f0c1e-0000-4000-8000-000000000001", "Broken signup page deep dive", "zerg", 0.5),
        live_count: 9,
        live_sessions: [
          live("5d6f0c1e-0000-4000-8000-000000000001", "Broken signup page deep dive", "zerg", 0.5),
          live("5d6f0c1e-0000-4000-8000-000000000002", "UI UX redesign for runners dashboard", "zerg", 0.8),
          live("5d6f0c1e-0000-4000-8000-000000000003", "Fix chat scroll capture on iOS", "zerg", 1.2),
          live("5d6f0c1e-0000-4000-8000-000000000004", "Recover OMP and Claude Sessions", "g55", 1.5),
          live("5d6f0c1e-0000-4000-8000-000000000005", "Athena image quality default change", "zeta", 6),
        ],
      },
      sync: sync(),
    },
    {
      machine: bench,
      activity: {
        ...empty(live("5d6f0c1e-0000-4000-8000-000000000010", "Provider omp handshake", "longhouse", 9 * 24 * 60)),
        sessions_started: 4,
        daily: daily([{}, {}, {}, {}, { omp: 4 }]),
        top_projects: [{ project: "longhouse", sessions: 4 }],
      },
      sync: sync({ engine_version: "0.1.50", upload_p95_ms: null, last_upload_at: at(9 * 24 * 60), history: { state: "current", source_count: 9, remaining_bytes: 0, remaining_records: 0, acknowledged_records: 412 } }),
    },
    { machine: cube, activity: empty(), sync: sync({ engine_version: "0.1.30", upload_p95_ms: null, last_upload_at: null, history: { state: "current", source_count: 4, remaining_bytes: 0, remaining_records: 0, acknowledged_records: 120 } }) },
    {
      machine: clifford,
      activity: {
        ...empty(live("5d6f0c1e-0000-4000-8000-000000000020", "crunch-runner reconnect connect error", "agent-sessions", 2 * 24 * 60, "opencode")),
        sessions_started: 8,
        daily: daily([{}, {}, { opencode: 1 }, { opencode: 2 }, {}, { opencode: 1 }, {}, {}, {}, { opencode: 1 }, { opencode: 2 }, { opencode: 1 }]),
        top_projects: [{ project: "agent-sessions", sessions: 8 }],
      },
      sync: null,
    },
    { machine: quietMachine("cube-canary", 82), activity: empty(), sync: null },
    { machine: quietMachine("drose-web-pepper", 8), activity: empty(), sync: null },
    { machine: quietMachine("github-cohort-journey", 61), activity: empty(), sync: null },
    { machine: quietMachine("sauron-clifford", 8), activity: empty(), sync: null },
  ];

  return {
    directory: { machines: machines.map((entry) => entry.machine) },
    summary: {
      generated_at: at(0.1),
      days: 14,
      utc_offset_minutes: 0,
      first_day: CALENDAR[0],
      last_day: CALENDAR[CALENDAR.length - 1],
      machines,
    },
    runners: [
      { id: 11, name: "cinder", status: "online", availability_policy: "on_demand", runner_version: "0.1.7", capabilities: ["exec.full"] },
      { id: 12, name: "clifford", status: "online", availability_policy: "always_on", runner_version: "0.1.7", capabilities: ["exec.full"] },
      { id: 13, name: "cube", status: "online", availability_policy: "on_demand", runner_version: "0.1.7", capabilities: ["exec.full"] },
      { id: 14, name: "lh-vm-canary-20260308", status: "offline", availability_policy: "ephemeral", runner_version: "0.1.0", capabilities: ["exec.readonly"] },
      { id: 15, name: "lh-vm-canary-20260309", status: "revoked", availability_policy: "ephemeral", runner_version: "0.1.0", capabilities: ["exec.readonly"] },
    ],
  };
}

/** The first-run host: one machine, connected minutes ago, still importing its history. */
export function buildFirstRunMachinesSummary(directory: { machines: Array<Record<string, unknown>> }) {
  return {
    generated_at: at(0.1),
    days: 14,
    utc_offset_minutes: 0,
    first_day: CALENDAR[0],
    last_day: CALENDAR[CALENDAR.length - 1],
    machines: directory.machines.map((entry) => ({
      machine: entry,
      activity: {
        ...empty(live("8a0d8a9e-0000-4000-8000-000000000001", "Add rate limiting to the upload…", "api", 60, "claude")),
        sessions_started: 5,
        daily: daily([{}, {}, {}, {}, {}, {}, {}, {}, {}, {}, { claude: 1 }, { codex: 1 }, { claude: 2 }, { codex: 1 }]),
        top_projects: [{ project: "api", sessions: 3 }],
      },
      sync: sync({
        engine_version: "0.1.62",
        history: { state: "importing", source_count: 412, remaining_bytes: 2_310_000_000, remaining_records: 58_000, acknowledged_records: 12_400 },
      }),
    })),
  };
}
