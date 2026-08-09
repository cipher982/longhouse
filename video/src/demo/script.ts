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
  { id: "steer", durSec: 6.5, caption: "Steer any of them, from anywhere." },
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
  /** 100x16 claude detail take — steer reaction in the export composition. */
  claude: { startSec: 3.5, endSec: 8.7 },
  /** 64x14 claude tile — beat-1 dense work window. */
  claudeTile: { startSec: 4.0, endSec: 8.7 },
  /** 64x14 claude tile — steer window: prompt lands ~4.8s, work streams to 9.3s. */
  claudeTileSteer: { startSec: 4.6, endSec: 9.0 },
  /** 64x14 codex tile — beat-1 dense work window. */
  codexTile: { startSec: 2.6, endSec: 7.5 },
} as const;

/** Beat 1 lineup: which providers show a real recorded tile. */
export const AGENT_TILES = [
  { providerId: "claude", recording: "claudeTile", window: REPLAY_WINDOWS.claudeTile },
  { providerId: "codex", recording: "codexTile", window: REPLAY_WINDOWS.codexTile },
] as const;

/* ── Steer beat choreography ─────────────────────────────────────────── */

export const STEER_MESSAGE =
  "Fix the off-by-one bug in count_items, then run the tests";

/** Seconds into the steer beat when the composer starts typing. */
export const STEER_TYPE_START_SEC = 0.7;
/** Composer typing speed (characters per second). */
export const STEER_CHARS_PER_SEC = 33;
/** Delay between Send firing and the terminal replay starting. */
export const STEER_REACT_DELAY_SEC = 0.27;

/** Seconds into the steer beat when the full message has been typed. */
export const steerSentAtSec = (): number =>
  STEER_TYPE_START_SEC + STEER_MESSAGE.length / STEER_CHARS_PER_SEC;

/** Frozen frame for reduced-motion / posters: steer beat, mid-reaction. */
export const POSTER_SEC = 12.5;
