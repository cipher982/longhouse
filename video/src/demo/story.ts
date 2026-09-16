/**
 * The hero story: one real session, handed off.
 *
 * Three recorded sessions work (Claude Code, Codex, OpenCode), dock into a
 * Longhouse timeline, and then the Claude session takes a follow-up from a
 * phone and does the work in the same recorded PTY.
 *
 * Pure data — no React, no Remotion. Every string a viewer reads comes from
 * a recording's metadata (the prompts it received) or from the fixture that
 * scripted its model turns (the replies it printed). Re-records change the
 * take-coupled numbers HERE and nowhere else.
 */
import claudeFixture from "../../scripts/terminal/fixtures/claude-handoff.json";
import codexFixture from "../../scripts/terminal/fixtures/codex-empty.json";
import opencodeFixture from "../../scripts/terminal/fixtures/opencode-hints.json";
import type { GridTimeline } from "../terminal/TerminalGrid";
import { claudeHandoff, codexTile, opencodeTile } from "./recordings";

/* ── Recording helpers ───────────────────────────────────────────────── */

export interface RecordedTurn {
  prompt: string;
  idleSec: number;
  typedSec: number;
}

interface TurnGridMeta {
  prompt?: string;
  promptIdleSec?: number;
  promptTypedSec?: number;
  turns?: Array<{ prompt: string; idleSec?: number; typedSec?: number }>;
}

/** Every prompt the recorded session received, with compile-time anchors. */
export function recordingTurns(grid: GridTimeline): RecordedTurn[] {
  const meta = grid.meta as TurnGridMeta;
  const raw = meta.turns ?? [
    { prompt: meta.prompt ?? "", idleSec: meta.promptIdleSec, typedSec: meta.promptTypedSec },
  ];
  return raw.map((turn) => {
    if (!turn.prompt || turn.idleSec === undefined || turn.typedSec === undefined) {
      throw new Error("recording has no prompt anchors — recompile its grid with compile.ts");
    }
    return { prompt: turn.prompt, idleSec: turn.idleSec, typedSec: turn.typedSec };
  });
}

const squash = (s: string) => s.replace(/\s+/g, "");

function stateText(grid: GridTimeline, index: number): string {
  const state = grid.states[index];
  return squash(state.rows.map((ri) => grid.rowPool[ri].map((run) => run.text).join("")).join(""));
}

/** First recording second at/after `fromSec` whose screen shows `text`. */
export function firstShownSec(grid: GridTimeline, text: string, fromSec: number): number {
  // Terminals wrap long replies, so match a leading slice that fits one row.
  const needle = squash(text).slice(0, 24);
  for (let i = 0; i < grid.states.length; i++) {
    if (grid.states[i].t >= fromSec && stateText(grid, i).includes(needle)) {
      return grid.states[i].t;
    }
  }
  throw new Error(`recording never shows ${JSON.stringify(text)} after ${fromSec}s`);
}

/* ── Fixture transcript → phone items ────────────────────────────────── */

interface FixtureTool {
  name: string;
  input: Record<string, unknown>;
}
interface Fixture {
  turns: Array<{ text?: string; tools?: FixtureTool[] }>;
}

export interface TranscriptItem {
  kind: "message" | "tool";
  text: string;
}

const baseName = (p: unknown) => String(p ?? "").split("/").pop() ?? "";

function toolLabel(tool: FixtureTool): string {
  const input = tool.input;
  switch (tool.name.toLowerCase()) {
    case "read":
      return `Read ${baseName(input.file_path ?? input.filePath)}`;
    case "edit":
      return `Edited ${baseName(input.file_path ?? input.filePath)}`;
    case "bash":
      return `Ran ${String(input.command)}`;
    default:
      return tool.name;
  }
}

/** Assistant replies + tool rows for fixture turns [from, to). */
function transcriptFor(fixture: Fixture, from: number, to: number): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  for (const turn of fixture.turns.slice(from, to)) {
    if (turn.text) items.push({ kind: "message", text: turn.text });
    const labels = (turn.tools ?? []).map(toolLabel);
    const counts = new Map<string, number>();
    for (const label of labels) counts.set(label, (counts.get(label) ?? 0) + 1);
    for (const [label, n] of counts) {
      items.push({ kind: "tool", text: n > 1 ? `${label} ×${n}` : label });
    }
  }
  return items;
}

const lastReply = (fixture: Fixture, before = fixture.turns.length) =>
  [...fixture.turns.slice(0, before)].reverse().find((t) => t.text)?.text ?? "";

/* ── The sessions ────────────────────────────────────────────────────── */

export interface HeroSession {
  id: "claude" | "codex" | "opencode";
  /** ProviderGlyph key. */
  glyph: string;
  name: string;
  accent: string;
  machine: string;
  /** Helm: launched through Longhouse, steerable. Shadow: observed only. */
  mode: "Helm" | "Shadow";
  timeline: GridTimeline;
  title: string;
  preview: string;
  /** Recording seconds replayed during the agents chapter. */
  window: { startSec: number; endSec: number };
  ago: string;
}

const CLAUDE_TURNS = recordingTurns(claudeHandoff);
/** claude-handoff.json: turns 0-3 answer prompt 1, turns 4-6 answer prompt 2. */
const CLAUDE_FIRST_REPLY_TURNS = 4;

export const HERO_SESSIONS: HeroSession[] = [
  {
    id: "claude",
    glyph: "claude",
    name: "Claude Code",
    accent: "#E8875A",
    machine: "macbook",
    mode: "Helm",
    timeline: claudeHandoff,
    title: CLAUDE_TURNS[0].prompt,
    preview: lastReply(claudeFixture, CLAUDE_FIRST_REPLY_TURNS),
    window: {
      startSec: CLAUDE_TURNS[0].typedSec + 0.05,
      endSec:
        firstShownSec(
          claudeHandoff,
          lastReply(claudeFixture, CLAUDE_FIRST_REPLY_TURNS),
          CLAUDE_TURNS[0].typedSec,
        ) + 0.3,
    },
    ago: "now",
  },
  {
    id: "codex",
    glyph: "codex",
    name: "Codex",
    accent: "#7BC9A8",
    machine: "devbox",
    mode: "Shadow",
    timeline: codexTile,
    title: recordingTurns(codexTile)[0].prompt,
    preview: lastReply(codexFixture),
    window: {
      startSec: recordingTurns(codexTile)[0].typedSec + 0.05,
      endSec: firstShownSec(codexTile, lastReply(codexFixture), 0) + 0.3,
    },
    ago: "1m ago",
  },
  {
    id: "opencode",
    glyph: "opencode",
    name: "OpenCode",
    accent: "#6FB7E8",
    machine: "homelab",
    mode: "Shadow",
    timeline: opencodeTile,
    title: recordingTurns(opencodeTile)[0].prompt,
    preview: lastReply(opencodeFixture),
    window: {
      startSec: recordingTurns(opencodeTile)[0].typedSec + 0.05,
      endSec: firstShownSec(opencodeTile, lastReply(opencodeFixture), 0) + 0.3,
    },
    ago: "3m ago",
  },
];

/* ── The handoff ─────────────────────────────────────────────────────── */

export interface TimedTranscriptItem extends TranscriptItem {
  /** Recording second at which the terminal first shows this item. */
  shownSec: number;
}

const followUp = CLAUDE_TURNS[1];

export const HANDOFF = {
  prompt: followUp.prompt,
  /**
   * Terminal holds here before Send: turn 1 done, composer empty. NOT the
   * follow-up's idle anchor: Claude Code fills an idle composer with a
   * prompt suggestion, and the mock answers that request with the next
   * scripted reply, so the anchor frame shows the future as ghost text.
   */
  holdSec: HERO_SESSIONS[0].window.endSec,
  /** A remote send lands as one paste: replay starts with it on screen. */
  replayStartSec: followUp.typedSec + 0.05,
  replayEndSec: firstShownSec(claudeHandoff, "Worked for", followUp.typedSec) + 0.2,
  /** What the phone already shows: turn 1, mirrored. */
  history: transcriptFor(claudeFixture, 0, CLAUDE_FIRST_REPLY_TURNS),
  /** Turn 2 items, each revealed when the terminal first shows it. */
  reply: transcriptFor(claudeFixture, CLAUDE_FIRST_REPLY_TURNS, claudeFixture.turns.length).map(
    (item, index, all): TimedTranscriptItem => {
      // Tool rows print right after the reply that introduces them.
      const anchor = item.kind === "message" ? item : [...all.slice(0, index)].reverse().find((i) => i.kind === "message");
      const shownSec = firstShownSec(claudeHandoff, anchor?.text ?? item.text, followUp.typedSec);
      return { ...item, shownSec: item.kind === "tool" ? shownSec + 0.2 : shownSec };
    },
  ),
  project: "demo-repo",
} as const;

/* ── Schedule (hero seconds) ─────────────────────────────────────────── */

export const HERO_TIMING = {
  /** Agents chapter: recordings start this long after the loop starts. */
  replayLeadSec: 0.2,
  dockStartSec: 4.5,
  dockDurSec: 1.0,
  /** Stagger between tiles docking. */
  dockStaggerSec: 0.1,
  handoffStartSec: 6.9,
  handoffInDurSec: 0.8,
  typeStartSec: 7.9,
  charsPerSec: 40,
  reactDelaySec: 0.3,
  /** Result hold after the replay ends, then fade to the loop start. */
  holdSec: 1.8,
  loopFadeSec: 0.45,
} as const;

export const HANDOFF_SENT_SEC =
  HERO_TIMING.typeStartSec + HANDOFF.prompt.length / HERO_TIMING.charsPerSec;

export const HANDOFF_REPLAY_START_SEC = HANDOFF_SENT_SEC + HERO_TIMING.reactDelaySec;

export const HERO_DURATION_SEC =
  HANDOFF_REPLAY_START_SEC +
  (HANDOFF.replayEndSec - HANDOFF.replayStartSec) +
  HERO_TIMING.holdSec;

export const HERO_CHAPTERS = [
  { id: "agents", startSec: 0, caption: "Your coding agents already run everywhere." },
  {
    id: "timeline",
    startSec: HERO_TIMING.dockStartSec,
    caption: "Longhouse puts every session in one timeline.",
  },
  {
    id: "handoff",
    startSec: HERO_TIMING.handoffStartSec,
    caption: "Steer the same session from your phone.",
  },
] as const;

/** Reduced-motion / poster frame: the follow-up mid-work. */
export const HERO_POSTER_SEC = HANDOFF_REPLAY_START_SEC + 1.2;
