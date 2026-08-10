/**
 * The demo script: single source of truth for the hero demo narrative.
 *
 * Pure data — no React, no Remotion. Two renderers consume it:
 *   - web/src/components/landing/demo/  (live DOM hero on the landing page)
 *   - video/src/compositions/ControlRoom.tsx  (mp4/OG export lane)
 *
 * Re-records change take-coupled numbers HERE and nowhere else.
 */

export interface DemoProvider {
  id: "claude" | "codex" | "cursor" | "opencode";
  name: string;
  cmd: string;
  color: string;
  machine: string;
  task: string;
}

export const PROVIDERS: DemoProvider[] = [
  {
    id: "claude",
    name: "Claude Code",
    cmd: "claude",
    color: "#E8875A",
    machine: "macbook",
    task: "Fix the inventory count bug",
  },
  {
    id: "codex",
    name: "Codex",
    cmd: "codex",
    color: "#7BC9A8",
    machine: "devbox",
    task: "Repair release build",
  },
  {
    id: "cursor",
    name: "Cursor Agent",
    cmd: "cursor-agent",
    color: "#9B8CFF",
    machine: "studio",
    task: "Migrate settings page",
  },
  {
    id: "opencode",
    name: "OpenCode",
    cmd: "opencode",
    color: "#6FB7E8",
    machine: "homelab",
    task: "Nightly digest job",
  },
];

/** Warm dark palette shared by both renderers. */
export const DEMO_PALETTE = {
  bg: "#0e0a08",
  panel: "#151009",
  chrome: "#1c1510",
  gold: "#C9A66B",
  cream: "#F3EAD9",
} as const;

/* ── Beat schedule ───────────────────────────────────────────────────── */

export type BeatId = "agents" | "unify" | "steer" | "close";

export interface Beat {
  id: BeatId;
  durSec: number;
  caption: string;
}

export const BEATS: Beat[] = [
  { id: "agents", durSec: 5, caption: "Your coding agents already run everywhere." },
  { id: "unify", durSec: 4, caption: "Longhouse normalizes all of them into one system." },
  { id: "steer", durSec: 8, caption: "Send the next instruction from anywhere." },
  { id: "close", durSec: 2.5, caption: "Remote control for your coding agents." },
];

/** Crossfade overlap between beats (both renderers use the same overlap). */
export const CROSSFADE_SEC = 0.5;

export interface BeatWindow extends Beat {
  startSec: number;
}

/** Beat start times with crossfade overlap applied. */
export function beatWindows(): BeatWindow[] {
  let offset = 0;
  return BEATS.map((beat) => {
    const w = { ...beat, startSec: offset };
    offset += beat.durSec - CROSSFADE_SEC;
    return w;
  });
}

/** Total demo duration in seconds. */
export const DEMO_DURATION_SEC =
  BEATS.reduce((acc, b) => acc + b.durSec, 0) - (BEATS.length - 1) * CROSSFADE_SEC;

/* ── Take-coupled replay windows ─────────────────────────────────────── */

/**
 * Absolute second-offsets into the SPECIFIC committed takes under
 * video/src/assets/terminal/. Every re-record: dump states (see the
 * landing-hero skill runbook) and re-pick these.
 */
export const REPLAY_WINDOWS = {
  /** 64x14 claude tile — beat-1 dense work window. */
  claudeTile: { startSec: 4.0, endSec: 8.7 },
  /** 64x14 codex tile — beat-1 dense work window. */
  codexTile: { startSec: 2.6, endSec: 7.5 },
} as const;

/* ── Steer windows: derived from the recording, never hand-picked ────── */

/** Editorial choice: where the steer replay stops (work has concluded). */
export const STEER_END_SEC = 9.0;
/** Small offset past the typed-anchor so the full prompt is on screen. */
const PASTE_SETTLE_SEC = 0.05;

interface SteerGridMeta {
  prompt?: string;
  promptIdleSec?: number;
  promptTypedSec?: number;
}

/**
 * The exact instruction the recorded session received. compile.ts stamps
 * it into the grid from the recorder's meta sidecar; the phone card MUST
 * display this — a hand-written display copy can drift from the footage
 * (that bug shipped once).
 */
export function recordingPrompt(grid: { meta: SteerGridMeta }): string {
  if (!grid.meta.prompt) {
    throw new Error(
      "recording has no prompt metadata — recompile its grid with compile.ts",
    );
  }
  return grid.meta.prompt;
}

/**
 * Steer replay window for a take, anchored to the recording's own derived
 * timestamps. A remote send lands in the PTY as one paste, not human
 * typing: pre-send the demo holds on `holdSec` (last frame with nothing
 * typed), and on Send it rolls from `startSec` (first frame with the full
 * prompt on screen) so the instruction arrives in one shot.
 */
export function steerWindow(
  grid: { meta: SteerGridMeta },
  endSec: number = STEER_END_SEC,
): { holdSec: number; startSec: number; endSec: number } {
  const { promptIdleSec, promptTypedSec } = grid.meta;
  if (promptIdleSec === undefined || promptTypedSec === undefined) {
    throw new Error(
      "recording has no prompt anchors — recompile its grid with compile.ts",
    );
  }
  return {
    holdSec: promptIdleSec,
    startSec: promptTypedSec + PASTE_SETTLE_SEC,
    endSec,
  };
}

/** Beat 1 lineup: which providers show a real recorded tile. */
export const AGENT_TILES = [
  { providerId: "claude", recording: "claudeTile", window: REPLAY_WINDOWS.claudeTile },
  { providerId: "codex", recording: "codexTile", window: REPLAY_WINDOWS.codexTile },
] as const;

/* ── Steer beat choreography ─────────────────────────────────────────── */

// The steer MESSAGE is not defined here: it comes from the recording via
// recordingPrompt() so the card can never drift from the footage.

/** Seconds into the steer beat when the composer starts typing. */
export const STEER_TYPE_START_SEC = 0.4;
/** Composer typing speed (characters per second). */
export const STEER_CHARS_PER_SEC = 46;
/** Delay between Send firing and the terminal replay starting. */
export const STEER_REACT_DELAY_SEC = 0.27;

/** Seconds into the steer beat when the full message has been typed. */
export const steerSentAtSec = (message: string): number =>
  STEER_TYPE_START_SEC + message.length / STEER_CHARS_PER_SEC;

/** Frozen frame for reduced-motion / posters: steer beat, mid-reaction. */
export const POSTER_SEC = 12.0;
