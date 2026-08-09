/**
 * Typed exports of the committed real-PTY grid timelines
 * (compiled by video/scripts/terminal/compile.ts).
 *
 * Named exports so bundlers only carry the takes a consumer imports:
 * the web hero uses only the 64-col tiles; the export composition also
 * uses the 100-col claude detail take.
 */
import type { GridTimeline } from "../terminal/TerminalGrid";

import claudeGridJson from "../assets/terminal/claude.grid.json";
import claudeTileJson from "../assets/terminal/claude-tile.grid.json";
import codexTileJson from "../assets/terminal/codex-tile.grid.json";

/** 100x16 Claude Code detail take. */
export const claudeGrid = claudeGridJson as unknown as GridTimeline;
/** 64x14 Claude Code tile take. */
export const claudeTile = claudeTileJson as unknown as GridTimeline;
/** 64x14 Codex tile take. */
export const codexTile = codexTileJson as unknown as GridTimeline;
