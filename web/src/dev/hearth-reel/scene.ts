/**
 * The hearth reel's script: four mock sessions whose card facts change on a
 * fixed timeline, fed to the real HearthLamp so the shipped fire renders
 * them. Pure and deterministic: the same t always yields the same cards, so
 * scripts/record-hearth-reel.mjs produces the same video on every run.
 *
 * Times are scene seconds. The recording covers [0, DURATION]; the fires
 * light during [-WARMUP, 0) so the first frame already shows them burning.
 */

import type { HearthMode, HearthSnapshot } from "@/shared/instruments/hearth/signals";
import type { StatusLampState } from "@/shared/instruments/StatusLamp";

export const WARMUP = 6;
export const DURATION = 20;

type Beat =
  | { at: number; tool: string }
  | { at: number; message: true }
  | { at: number; prompt: true }
  | { at: number; state: StatusLampState; mode: HearthMode; label: string };

export interface Station {
  key: string;
  title: string;
  provider: string;
  /** Initial card state before the first beat. */
  state: StatusLampState;
  mode: HearthMode;
  label: string;
  toolCalls: number;
  subagents: number;
  startedAgoS: number;
  /** Seconds before scene start of the last activity (idle/waiting coal age). */
  idleAgoS: number;
  beats: Beat[];
}

export interface StationView {
  key: string;
  title: string;
  provider: string;
  detail: string;
  state: StatusLampState;
  label: string;
  snapshot: HearthSnapshot;
}

const working = (at: number, label: string): Beat => ({ at, state: "working", mode: "working", label });

/** A run of calls to one tool, every `gap` seconds from `at`. */
function burst(at: number, tool: string, n: number, gap: number): Beat[] {
  const out: Beat[] = [working(at, `Using ${tool}`)];
  for (let i = 0; i < n; i++) out.push({ at: at + 0.15 + i * gap, tool });
  return out;
}

function thinking(at: number): Beat[] {
  return [working(at, "Thinking"), { at: at + 0.6, message: true }];
}

export const STATIONS: Station[] = [
  {
    // A busy session: bursts of shell, edit and read calls with thinking between.
    key: "reel-busy",
    title: "Fix flaky auth redirect test",
    provider: "claude",
    state: "working",
    mode: "working",
    label: "Thinking",
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
    key: "reel-agents",
    title: "Migrate billing webhooks",
    provider: "codex",
    state: "working",
    mode: "working",
    label: "Thinking",
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
    key: "reel-waiting",
    title: "Release v0.1.56",
    provider: "claude",
    state: "waiting",
    mode: "waiting",
    label: "Waiting on you",
    toolCalls: 88,
    subagents: 0,
    startedAgoS: 3600,
    idleAgoS: 45,
    beats: [],
  },
  {
    // Idle coals; a prompt stokes it, it works, then settles back to coals.
    key: "reel-idle",
    title: "Refactor search index",
    provider: "cursor",
    state: "idle",
    mode: "idle",
    label: "Idle",
    toolCalls: 60,
    subagents: 0,
    startedAgoS: 5400,
    idleAgoS: 240,
    beats: [
      { at: 4, prompt: true },
      ...thinking(4),
      ...burst(5.4, "Read", 3, 0.5),
      ...burst(7.4, "Edit", 2, 0.8),
      ...burst(9.2, "Bash", 2, 0.6),
      { at: 10.8, message: true },
      // Idle when the flame actually drops (ACTIVE_HOLD after the last event), so label and fire agree.
      { at: 15.6, state: "idle", mode: "idle", label: "Idle" },
    ],
  },
];

for (const s of STATIONS) s.beats.sort((a, b) => a.at - b.at);

/** The station's card at scene time t; `wallAt(s)` maps scene seconds to wall ms. */
export function stationAt(s: Station, t: number, wallAt: (sceneS: number) => number): StationView {
  let state = s.state;
  let mode = s.mode;
  let label = s.label;
  let toolCalls = s.toolCalls;
  let assistantMessages = Math.round(s.toolCalls * 0.6);
  let userMessages = 4;
  let tool: string | null = null;
  let lastActivity = s.mode === "working" ? -WARMUP : -WARMUP - s.idleAgoS;
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
      state = b.state;
      mode = b.mode;
      label = b.label;
      if (!b.label.startsWith("Using ")) tool = null;
    }
  }
  const detail =
    mode === "working" && s.subagents > 0
      ? `${s.subagents} subagents running`
      : mode === "waiting"
        ? "Asked: ship it to production?"
        : mode === "idle" && t < 4 && s.key === "reel-idle"
          ? "Last active 4 min ago"
          : mode === "idle"
            ? "Just finished"
            : "";
  return {
    key: s.key,
    title: s.title,
    provider: s.provider,
    detail,
    state,
    label,
    snapshot: {
      mode,
      toolCalls,
      assistantMessages,
      userMessages,
      subagents: mode === "working" ? s.subagents : 0,
      tool: mode === "working" ? tool : null,
      lastActivityMs: wallAt(lastActivity),
      startedMs: wallAt(-WARMUP - s.startedAgoS),
    },
  };
}
