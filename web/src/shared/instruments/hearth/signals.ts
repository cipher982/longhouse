/**
 * Hearth signal model: what the timeline card says a session is doing,
 * turned into fuel, sparks, stokes and a cooling coal bed.
 *
 * Pure CPU and deterministic given its inputs, so it is unit-tested apart
 * from the WebGL renderer (renderer.ts), which only reads the numbers here.
 *
 * Deliberately coarse (David, 2026-09-27): idle, working and very busy must
 * read apart at a glance; exact rates must not. There is no token signal on
 * the served card (Claude's turn usage is written only on the turn-ending
 * reply), so work is inferred from what the card does carry:
 *   - activity state (thinking / executing)    -> a base flame while working
 *   - tool_calls / assistant_messages deltas    -> flares, sparks, work
 *   - lifetime tool density                     -> a busy session starts busy
 *   - user_messages delta                       -> a stoke
 *   - delegation.count                          -> extra flame roots
 *   - last_activity_at                          -> Newton cooling of the bed
 */

import type { AgentSession } from "@/shared/api/agents";
import { executingToolName } from "@/shared/session/sessionStatus";

export type HearthMode = "working" | "waiting" | "idle" | "ended";
export type ToolKind = "exec" | "edit" | "read" | "agent" | "other";

/** The card facts the fire reads. Plain values so a signature is cheap. */
export interface HearthSnapshot {
  mode: HearthMode;
  toolCalls: number;
  assistantMessages: number;
  userMessages: number;
  subagents: number;
  /** Tool running now, when the activity observation names one. */
  tool: string | null;
  /** Wall-clock ms of the session's last activity (server fact). */
  lastActivityMs: number | null;
  /** Wall-clock ms the session started. */
  startedMs: number | null;
}

export function snapshotSignature(s: HearthSnapshot): string {
  return [s.mode, s.toolCalls, s.assistantMessages, s.userMessages, s.subagents, s.tool ?? "", s.lastActivityMs ?? "", s.startedMs ?? ""].join("|");
}

// ---- constants (seconds unless noted) ----
export const T_AMB = 300;
export const T_HOT = 1350;
/** Bed temperature when a working session stops. */
export const T_LIT = 1150;
export const TAU_BED = 1200; // 20 min: the surface goes orange -> red -> black
export const TAU_CORE = 10800; // 3 h: embers deep in the cracks
const TAU_HEAT = 25;
const TAU_SOAK = 300;
/** Coals stay warm while a session waits on its person. */
export const T_WAITING = 1020;

export const H_FLOOR = 0.42;
export const H_TOP = 0.8;
export const WORK_SCALE = 60;
const WORK_TAU = 8;
/** Seconds after the last observed event before an otherwise idle session goes quiet. */
export const ACTIVE_HOLD = 5;
/** Token-equivalents credited per tool call and per assistant message. */
export const TOOL_WORK = 150;
export const MSG_WORK = 90;
/** Base work while the activity observation says the agent is running. */
export const BASE_WORK: Record<HearthMode, number> = { working: 18, waiting: 0, idle: 0, ended: 0 };
/** Work credited per tool/second of lifetime density, capped. */
const DENSITY_WORK = 0.5 * TOOL_WORK;
const DENSITY_CAP = 90;
/** A batch of events arriving in one card update plays out over this long. */
const SPREAD_MAX = 2.5;
/** At most this many events per batch become sparks; the rest still count as work. */
const MAX_VISIBLE_EVENTS = 8;
/** Events credited as work per batch (a reconnect can report hundreds). */
const MAX_CREDITED_EVENTS = 24;

/** Flame height as a fraction of the tile. Any active session gets a living
 * pilot flame; work pushes it up a saturating curve so a busy session looks
 * like a proper fire most of the time. */
export function targetHeight(work: number, active: boolean): number {
  if (!active) return 0;
  return H_FLOOR + (H_TOP - H_FLOOR) * (1 - Math.exp(-Math.max(0, work) / WORK_SCALE));
}

export function kindOf(name: string | null | undefined): ToolKind {
  const n = (name ?? "").toLowerCase().replace(/^mcp__[^_]+__/, "");
  if (/^(bash|shell|exec|run|terminal|command)/.test(n)) return "exec";
  if (/^(edit|write|multiedit|notebookedit|apply_patch|patch|str_replace)/.test(n)) return "edit";
  if (/^(read|grep|glob|find|search|ls|view|web_?fetch|fetch)/.test(n)) return "read";
  if (/^(agent|task|spawn|hatch)/.test(n)) return "agent";
  return "other";
}

/** Newton cooling toward ambient. */
export function cool(T0: number, seconds: number, tau: number): number {
  return T_AMB + (T0 - T_AMB) * Math.exp(-Math.max(0, seconds) / tau);
}

/** Bed temperatures for a session seen for the first time, from its idle age. */
export function initialBed(snap: HearthSnapshot, nowMs: number): { surface: number; core: number } {
  if (snap.mode === "ended") return { surface: T_AMB, core: T_AMB };
  if (snap.mode === "working") return { surface: T_LIT, core: T_LIT };
  const idle = snap.lastActivityMs == null ? Infinity : (nowMs - snap.lastActivityMs) / 1000;
  if (!Number.isFinite(idle)) return { surface: T_AMB, core: T_AMB };
  const surface = cool(T_LIT, idle, TAU_BED);
  const core = cool(T_LIT, idle, TAU_CORE);
  if (snap.mode === "waiting") return { surface: Math.max(surface, T_WAITING), core: Math.max(core, T_WAITING) };
  return { surface, core };
}

export interface CardDelta {
  tools: number;
  messages: number;
  prompts: number;
}

/** New tool calls, replies and prompts between two card updates. A count
 * that goes down (a different head, a reset) is a new baseline, not events. */
export function diffSnapshots(prev: HearthSnapshot | null, next: HearthSnapshot): CardDelta {
  if (!prev) return { tools: 0, messages: 0, prompts: 0 };
  const d = (a: number, b: number) => (b > a ? b - a : 0);
  return {
    tools: d(prev.toolCalls, next.toolCalls),
    messages: d(prev.assistantMessages, next.assistantMessages),
    prompts: d(prev.userMessages, next.userMessages),
  };
}

/** Lifetime tool density as work: a session that has fired a tool every few
 * seconds for an hour starts as a busy fire rather than a pilot light. */
export function densityWork(snap: HearthSnapshot): number {
  if (snap.startedMs == null || snap.lastActivityMs == null) return 0;
  const span = (snap.lastActivityMs - snap.startedMs) / 1000;
  if (span < 60 || snap.toolCalls <= 0) return 0;
  return Math.min(DENSITY_CAP, (snap.toolCalls / span) * DENSITY_WORK);
}

export type HearthEvent =
  | { type: "tool"; kind: ToolKind; name: string | null }
  | { type: "message" }
  | { type: "prompt" };

interface Deposit {
  end: number;
  rate: number;
}

interface Pending {
  at: number;
  event: HearthEvent;
}

/** Per-session heat, kept by the renderer across row remounts. Times are in
 * seconds on the caller's monotonic clock; wall-clock ms only for cooling. */
export class SessionHeat {
  snap: HearthSnapshot | null = null;
  work = 0;
  surface = T_AMB;
  core = T_AMB;
  /** Bed temperatures when activity last stopped; cooling runs from here. */
  private surface0 = T_AMB;
  private core0 = T_AMB;
  private deposits: Deposit[] = [];
  private pending: Pending[] = [];
  private lastEventT = -Infinity;
  private lastUpdateT = -Infinity;
  /** Wall ms of the last event this client saw (server facts can lag). */
  private lastEventWallMs: number | null = null;
  private wasActive = false;

  /** Apply a new card snapshot. Returns the delta it found (for tests/debug). */
  update(next: HearthSnapshot, t: number, wallMs: number): CardDelta {
    const prev = this.snap;
    const delta = diffSnapshots(prev, next);
    this.snap = next.tool == null && prev?.tool ? { ...next, tool: prev.tool } : next;
    if (!prev) {
      // Cooling runs once, from the last activity: the bed was lit then
      // (cold ash if ended), and coolBed() carries it to now.
      const bed = initialBed(next, wallMs);
      this.surface = bed.surface;
      this.core = bed.core;
      this.surface0 = this.core0 = next.mode === "ended" ? T_AMB : T_LIT;
      this.wasActive = next.mode === "working";
      this.work = next.mode === "working" ? BASE_WORK.working + densityWork(next) : 0;
      this.lastUpdateT = t;
      return delta;
    }
    const since = Number.isFinite(this.lastUpdateT) ? t - this.lastUpdateT : SPREAD_MAX;
    this.lastUpdateT = t;
    const spread = Math.max(0.3, Math.min(SPREAD_MAX, since));
    // Cap before building anything: a reconnect can report thousands.
    // Prompts lead the batch (the stoke comes before the work it caused).
    const prompts = Math.min(delta.prompts, MAX_CREDITED_EVENTS);
    const tools = Math.min(delta.tools, MAX_CREDITED_EVENTS - prompts);
    const messages = Math.min(delta.messages, MAX_CREDITED_EVENTS - prompts - tools);
    const credited: HearthEvent[] = [];
    for (let i = 0; i < prompts; i++) credited.push({ type: "prompt" });
    // The card names only the tool running now; a batch that finished while
    // the agent went back to thinking keeps the last tool it named.
    const toolName = next.tool ?? prev.tool;
    const kind = kindOf(toolName);
    for (let i = 0; i < tools; i++) credited.push({ type: "tool", kind, name: toolName });
    for (let i = 0; i < messages; i++) credited.push({ type: "message" });
    const shown = credited.length > MAX_VISIBLE_EVENTS ? thin(credited, MAX_VISIBLE_EVENTS) : credited;
    const extra = credited.filter((e) => !shown.includes(e));
    shown.forEach((event, i) => {
      this.pending.push({ at: t + (spread * i) / Math.max(1, shown.length), event });
    });
    // Unshown events still feed the fire.
    for (const event of extra) this.deposit(event, t);
    if (credited.length) {
      this.lastEventWallMs = wallMs;
    }
    return delta;
  }

  private deposit(event: HearthEvent, t: number) {
    if (event.type === "tool") this.deposits.push({ end: t + 4, rate: TOOL_WORK / 4 });
    else if (event.type === "message") this.deposits.push({ end: t + 4, rate: MSG_WORK / 4 });
  }

  /** Advance to time t (seconds); due events are handed to `emit`. */
  step(t: number, dt: number, wallMs: number, emit?: (e: HearthEvent) => void): void {
    const snap = this.snap;
    if (!snap) return;
    if (this.pending.length) {
      const due = this.pending.filter((p) => p.at <= t);
      if (due.length) {
        this.pending = this.pending.filter((p) => p.at > t);
        for (const p of due) {
          this.deposit(p.event, t);
          this.lastEventT = t;
          if (p.event.type === "prompt") this.surface += 0.3 * (T_HOT - this.surface);
          if (p.event.type === "tool") this.surface += 0.03 * (T_HOT - this.surface);
          emit?.(p.event);
        }
      }
    }
    this.deposits = this.deposits.filter((d) => d.end > t);
    let r = 0;
    for (const d of this.deposits) r += d.rate;
    const active = this.isActive(t);
    const base = active && snap.mode === "working" ? BASE_WORK.working + densityWork(snap) : 0;
    const k = 1 - Math.exp(-dt / WORK_TAU);
    this.work += (r + base - this.work) * k;

    if (snap.mode === "ended") {
      this.surface = this.core = this.surface0 = this.core0 = T_AMB;
      this.wasActive = false;
      return;
    }
    if (active) {
      // Heat toward a working bed; soak the core.
      const P = 0.35 + Math.min(0.6, r / 200);
      this.surface += (T_HOT - this.surface) * (1 - Math.exp((-P * dt) / TAU_HEAT));
      if (this.surface > this.core) this.core += (this.surface - this.core) * (1 - Math.exp(-dt / TAU_SOAK));
      this.wasActive = true;
      return;
    }
    if (this.wasActive) {
      this.surface0 = this.surface;
      this.core0 = Math.max(this.core, this.surface);
      this.wasActive = false;
      this.lastEventWallMs = Math.max(this.lastEventWallMs ?? 0, wallMs);
    }
    const bed = this.coolBed(wallMs);
    this.surface = bed.surface;
    this.core = bed.core;
  }

  /** Closed-form cooling from the last activity, so a bed cools correctly
   * while the render loop is stopped. */
  coolBed(wallMs: number): { surface: number; core: number } {
    const snap = this.snap;
    if (!snap) return { surface: T_AMB, core: T_AMB };
    const last = Math.max(snap.lastActivityMs ?? 0, this.lastEventWallMs ?? 0);
    const idle = last > 0 ? (wallMs - last) / 1000 : Infinity;
    let surface = Number.isFinite(idle) ? cool(this.surface0, idle, TAU_BED) : T_AMB;
    let core = Number.isFinite(idle) ? cool(this.core0, idle, TAU_CORE) : T_AMB;
    if (snap.mode === "waiting") {
      surface = Math.max(surface, T_WAITING);
      core = Math.max(core, T_WAITING);
    }
    return { surface, core };
  }

  isActive(t: number): boolean {
    const snap = this.snap;
    if (!snap || snap.mode === "ended") return false;
    return snap.mode === "working" || this.pending.length > 0 || this.deposits.length > 0 || t - this.lastEventT < ACTIVE_HOLD;
  }

  /** Flame height this session is asking for right now. */
  target(t: number): number {
    return targetHeight(this.work, this.isActive(t));
  }

  hasPending(): boolean {
    return this.pending.length > 0;
  }
}

/** Keep n evenly spaced items, always keeping prompts. */
function thin<T extends HearthEvent>(items: T[], n: number): T[] {
  const prompts = items.filter((e) => e.type === "prompt");
  const rest = items.filter((e) => e.type !== "prompt");
  const room = Math.max(0, n - prompts.length);
  const out: T[] = [...prompts];
  for (let i = 0; i < room && rest.length; i++) out.push(rest[Math.floor((i * rest.length) / room)]);
  return out;
}

/** Row lamp state -> fire mode. Unread results and unknown activity are
 * coals: the label beside the fire says which. */
export function hearthModeForLamp(state: "working" | "waiting" | "idle" | "unknown" | "ended" | "done" | "failed"): HearthMode {
  switch (state) {
    case "working":
      return "working";
    case "waiting":
      return "waiting";
    case "ended":
      return "ended";
    default:
      return "idle";
  }
}

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** The fire's inputs from a timeline session, given the row's lamp mode. */
export function hearthSnapshotFromSession(
  session: Pick<AgentSession, "tool_calls" | "assistant_messages" | "user_messages" | "session_state" | "last_activity_at" | "started_at">,
  mode: HearthMode,
): HearthSnapshot {
  const activity = session.session_state.activity;
  return {
    mode,
    toolCalls: session.tool_calls ?? 0,
    assistantMessages: session.assistant_messages ?? 0,
    userMessages: session.user_messages ?? 0,
    subagents: session.session_state.delegation?.count ?? 0,
    tool: executingToolName(activity),
    lastActivityMs: parseMs(session.last_activity_at),
    startedMs: parseMs(session.started_at),
  };
}
