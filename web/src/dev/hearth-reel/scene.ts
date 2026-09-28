/**
 * The hearth reel's script: mock timeline cards whose facts change on a fixed
 * timeline, rendered by the real TimelineInbox (rows, shelf, History, the
 * shipped fire). Pure and deterministic: the same t always yields the same
 * cards, so scripts/record-hearth-reel.mjs produces the same video every run.
 *
 * Times are scene seconds. The recording covers [0, DURATION]; the fires
 * light during [-WARMUP, 0) so the first frame already shows them burning.
 */

import type { AgentSession, SessionStateFacts, TimelineSessionCard } from "@/shared/api/agents";
import { makeSessionStateFacts } from "@/shared/test/sessionState";

export const WARMUP = 6;
export const DURATION = 20;

type Activity = "thinking" | "executing" | "quiescent" | "waiting";

type Beat =
  | { at: number; tool: string }
  | { at: number; message: true }
  | { at: number; prompt: true }
  | { at: number; activity: Activity };

interface Script {
  id: string;
  title: string;
  /** The row's grey second line. */
  summary: string;
  provider: string;
  project: string;
  branch: string | null;
  mode: SessionStateFacts["mode"];
  /** Activity before the first beat; null is a closed session. */
  activity: Activity | null;
  toolCalls: number;
  subagents: number;
  startedAgoS: number;
  /** Seconds before scene start of the last activity (coal age). */
  idleAgoS: number;
  beats: Beat[];
}

/** A run of calls to one tool, every `gap` seconds from `at`. */
function burst(at: number, tool: string, n: number, gap: number): Beat[] {
  const out: Beat[] = [{ at, activity: "executing" }];
  for (let i = 0; i < n; i++) out.push({ at: at + 0.15 + i * gap, tool });
  return out;
}

function thinking(at: number): Beat[] {
  return [{ at, activity: "thinking" }, { at: at + 0.6, message: true }];
}

const SCRIPTS: Script[] = [
  {
    // Busy: bursts of shell, edit and read calls with thinking between.
    id: "reel-busy",
    title: "Fix flaky auth redirect test",
    summary: "the login redirect test fails about 1 in 5 on CI, find out why and fix it",
    provider: "claude",
    project: "longhouse",
    branch: "fix/auth-redirect",
    mode: "helm",
    activity: "thinking",
    toolCalls: 420,
    subagents: 0,
    startedAgoS: 1800,
    idleAgoS: 0,
    beats: [
      ...burst(-5, "Bash", 4, 0.7),
      ...thinking(-2),
      ...burst(-0.6, "Edit", 3, 0.9),
      ...thinking(2.4),
      ...burst(3.6, "Bash", 5, 0.6),
      ...burst(7.0, "Read", 4, 0.5),
      ...thinking(9.2),
      ...burst(10.6, "Edit", 3, 0.8),
      ...burst(13.2, "Bash", 6, 0.55),
      ...thinking(16.8),
      ...burst(18.0, "Bash", 3, 0.6),
    ],
  },
  {
    // Delegating: three subagents are extra flame roots.
    id: "reel-agents",
    title: "Migrate billing webhooks to v2",
    summary: "split the webhook handlers across three agents and migrate each to the v2 events",
    provider: "codex",
    project: "billing",
    branch: "webhooks-v2",
    mode: "helm",
    activity: "thinking",
    toolCalls: 140,
    subagents: 3,
    startedAgoS: 2400,
    idleAgoS: 0,
    beats: [
      ...burst(-5.5, "Task", 3, 0.5),
      ...thinking(-3),
      ...[1.5, 5.5, 9.5, 13.5, 17.5].flatMap((at) => [...burst(at, "Read", 2, 0.6), ...thinking(at + 1.6)]),
    ],
  },
  {
    // Blocked on its person: a low guttering flame over warm coals.
    id: "reel-waiting",
    title: "Release v0.1.56",
    summary: "cut the release from the soaked SHA once dogfood has held for 24 hours",
    provider: "claude",
    project: "longhouse",
    branch: "main",
    mode: "helm",
    activity: "waiting",
    toolCalls: 88,
    subagents: 0,
    startedAgoS: 3000,
    idleAgoS: 45,
    beats: [],
  },
  {
    // Idle coals; a prompt stokes it, it works, then settles back to coals.
    id: "reel-idle",
    title: "Refactor search index",
    summary: "move the FTS rebuild off the request path",
    provider: "cursor",
    project: "longhouse",
    branch: "search-index",
    mode: "helm",
    activity: "quiescent",
    toolCalls: 60,
    subagents: 0,
    startedAgoS: 3600,
    idleAgoS: 240,
    beats: [
      { at: 4, prompt: true },
      ...thinking(4),
      ...burst(5.4, "Read", 3, 0.5),
      ...burst(7.4, "Edit", 2, 0.8),
      ...burst(9.2, "Bash", 2, 0.6),
      { at: 10.8, message: true },
      // Idle when the flame actually drops (ACTIVE_HOLD after the last event), so label and fire agree.
      { at: 15.6, activity: "quiescent" },
    ],
  },
  {
    // Steady reading: a research turn that mostly reads and searches.
    id: "reel-reading",
    title: "Why does the iOS dock lag on resume",
    summary: "profile the resume path and find what blocks the first frame",
    provider: "omp",
    project: "longhouse",
    branch: "ios-resume",
    mode: "helm",
    activity: "thinking",
    toolCalls: 230,
    subagents: 0,
    startedAgoS: 1200,
    idleAgoS: 0,
    beats: [
      ...thinking(-4),
      ...[-2, 2, 6.5, 11, 15.5].flatMap((at) => [...burst(at, "Grep", 3, 0.7), ...thinking(at + 2.4)]),
    ],
  },
  {
    id: "reel-history-1",
    title: "Timeline row hover prefetch",
    summary: "prefetch the transcript when the pointer rests on a row",
    provider: "claude",
    project: "longhouse",
    branch: null,
    mode: "helm",
    activity: null,
    toolCalls: 210,
    subagents: 0,
    startedAgoS: 3 * 3600,
    idleAgoS: 2 * 3600,
    beats: [],
  },
  {
    id: "reel-history-2",
    title: "Nightly backup restore drill",
    summary: "restore last night's snapshot into a scratch tenant and diff it",
    provider: "codex",
    project: "longhouse",
    branch: null,
    mode: "console",
    activity: null,
    toolCalls: 95,
    subagents: 0,
    startedAgoS: 5 * 3600,
    idleAgoS: 4 * 3600,
    beats: [],
  },
];

for (const s of SCRIPTS) s.beats.sort((a, b) => a.at - b.at);

const iso = (ms: number) => new Date(ms).toISOString();

/** The timeline cards at scene time t; `wallAt(s)` maps scene seconds to wall ms. */
export function cardsAt(t: number, wallAt: (sceneS: number) => number, gen: number): TimelineSessionCard[] {
  return SCRIPTS.map((s) => {
    let activity = s.activity;
    let toolCalls = s.toolCalls;
    let assistantMessages = Math.round(s.toolCalls * 0.6);
    let userMessages = 4;
    let tool: string | null = null;
    let lastActivity = activity === "thinking" || activity === "executing" ? -WARMUP : -WARMUP - s.idleAgoS;
    for (const b of s.beats) {
      if (b.at > t) break;
      if ("tool" in b) {
        toolCalls++;
        tool = b.tool;
        lastActivity = b.at;
      } else if ("message" in b) {
        assistantMessages++;
        lastActivity = b.at;
      } else if ("prompt" in b) {
        userMessages++;
        lastActivity = b.at;
      } else {
        activity = b.activity;
      }
    }
    const closed = activity === null;
    const observedAt = iso(wallAt(lastActivity));
    const state = makeSessionStateFacts({
      closed,
      access: closed ? "search_only" : "live_control",
      mode: s.mode,
      activity: activity === "waiting" || activity === null ? "quiescent" : activity,
      pendingInteraction: activity === "waiting",
      tool: activity === "executing" ? tool : null,
      terminalAttached: closed ? null : true,
      observedAt,
    });
    if (activity === "executing" && tool && state.presentation.primary) state.presentation.primary.label = `Using ${tool}`;
    if (!closed && (activity === "thinking" || activity === "executing") && s.subagents > 0) {
      state.delegation = { state: "pending", count: s.subagents, kinds: { subagent: s.subagents } };
    }
    const id = `${s.id}-${gen}`;
    const head = {
      id,
      provider: s.provider,
      started_at: iso(wallAt(-WARMUP - s.startedAgoS)),
      ended_at: closed ? observedAt : null,
      last_activity_at: observedAt,
      timeline_anchor_at: observedAt,
      project: s.project,
      cwd: `/Users/you/git/${s.project}`,
      git_repo: s.project,
      git_branch: s.branch,
      device_id: "device-cinder",
      summary_title: s.title,
      summary: s.summary,
      first_user_message: s.summary,
      user_messages: userMessages,
      assistant_messages: assistantMessages,
      tool_calls: toolCalls,
      terminal_state: null,
      session_state: state,
      timeline_card: null,
    } as unknown as AgentSession;
    return {
      thread_id: id,
      timeline_anchor_at: observedAt,
      head,
      continuation_count: 1,
      started_origin_label: null,
      head_origin_label: null,
    };
  });
}
