import type { GridTimeline } from "@longhouse/video/demo";

/**
 * Derive the phone transcript from the RECORDING, not from hand-written
 * copy: scan the grid timeline for the Claude Code "⏺" blocks (tool calls
 * and prose) and timestamp each by its first on-screen appearance. The
 * phone then mirrors the session exactly the way the real Longhouse iOS
 * app mirrors a live session — same provenance rule as the chip labels.
 */

export interface SessionEvent {
  tSec: number;
  kind: "tool" | "assistant";
  /** Tool name ("Read", "Bash") or the full prose text. */
  title: string;
  /** Tool argument/detail line. */
  subtitle?: string;
  /** Tool result line (from the "⎿" row), e.g. "all tests passed". */
  result?: string;
}

const TOOL_HEAD = /^(Read|Write|Edit|Update|Bash|Search|Grep|Glob|Task)\b/;
const NOISE = /\(ctrl\+o to expand\)/g;

const strip = (s: string) => s.replace(/\s+/g, "");

interface Block {
  tSec: number;
  body: string;
  result?: string;
}

export function extractSessionEvents(grid: GridTimeline): SessionEvent[] {
  const rowText = (idx: number) =>
    grid.rowPool[idx]?.map((run) => run.text).join("") ?? "";

  // Keyed by a stable prefix of the block's first line (text streams
  // left-to-right, so the prefix is stable once long enough).
  const blocks = new Map<string, Block>();

  for (const state of grid.states) {
    const texts = state.rows.map(rowText);
    for (let i = 0; i < texts.length; i++) {
      const line = texts[i].trim();
      if (!line.startsWith("⏺")) continue;
      let body = line.slice(1).trim();
      if (!body) continue;
      const key = strip(body).slice(0, 18);
      if (key.length < 6) continue;

      // Continuation rows: indented wrapped prose beneath the block line.
      for (let j = i + 1; j < texts.length; j++) {
        const raw = texts[j];
        const trimmed = raw.trim();
        if (!trimmed || !/^\s{1,}/.test(raw)) break;
        if (/^[⏺⎿✻❯─]/.test(trimmed) || trimmed.startsWith("⏵")) break;
        body += ` ${trimmed}`;
      }

      // Result row ("⎿ all tests passed") directly under the block.
      let result: string | undefined;
      const next = texts[i + 1]?.trim();
      if (next?.startsWith("⎿")) {
        result = next.slice(1).trim() || undefined;
      }

      const existing = blocks.get(key);
      if (!existing) {
        blocks.set(key, { tSec: state.t, body, result });
      } else {
        // Keep the fullest text/result seen (streaming grows them).
        if (body.length > existing.body.length) existing.body = body;
        if (result && (!existing.result || result.length > existing.result.length)) {
          existing.result = result;
        }
      }
    }
  }

  const events: SessionEvent[] = [];
  for (const block of blocks.values()) {
    const body = block.body.replace(NOISE, "").replace(/\s+/g, " ").trim();
    const toolMatch = body.match(TOOL_HEAD);
    if (toolMatch) {
      const name = toolMatch[1];
      const paren = body.match(/^\w+\(([^)]*)\)/);
      const subtitle = paren
        ? paren[1]
        : body.slice(name.length).trim() || undefined;
      events.push({
        tSec: block.tSec,
        kind: "tool",
        title: name,
        subtitle: subtitle || block.result,
        result: block.result,
      });
    } else {
      events.push({ tSec: block.tSec, kind: "assistant", title: body });
    }
  }
  return events.sort((a, b) => a.tSec - b.tSec);
}
