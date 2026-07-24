/**
 * Edit-shape detection and diff stats for the timeline (see
 * `docs/specs/transcript-action-visibility.md`).
 *
 * A collapsed row must be able to say *which file changed and how much* without
 * a click, so stats are needed from `formatActivitySummary()` — which runs on
 * every render. `lineDiff()` is O(n*m), so every computation is memoized per
 * interaction and guarded by a cell budget checked *before* the LCS runs.
 */

import type { ToolInteraction } from "./types";
import { lineDiff } from "./diff";

/** Max LCS cells (old lines × new lines) we will spend on one interaction. */
export const DIFF_CELL_BUDGET = 250_000;

export type EditPatch =
  | { kind: "replace"; oldStr: string; newStr: string }
  | { kind: "create"; content: string }
  | { kind: "delete"; content: string }
  | { kind: "patch"; text: string };

export type EditStat = {
  filePath: string | null;
  /** Basename for collapsed headers; full path stays in the expanded body. */
  fileName: string | null;
  added: number;
  removed: number;
  /** False when the shape is unknown or the diff exceeded the budget. */
  hasStat: boolean;
  patch: EditPatch | null;
};

const NO_STAT: EditStat = {
  filePath: null,
  fileName: null,
  added: 0,
  removed: 0,
  hasStat: false,
  patch: null,
};

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function basename(path: string | null): string | null {
  if (!path) return null;
  const parts = path.split("/").filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}

function countLines(text: string): number {
  return text === "" ? 0 : text.split("\n").length;
}

/** Path field names vary across providers (Claude, Codex, OpenCode, Cursor). */
function editFilePath(input: Record<string, unknown>): string | null {
  return (
    str(input.file_path) ??
    str(input.path) ??
    str(input.filePath) ??
    str(input.filename) ??
    null
  );
}

/**
 * Classify an edit-tool input into one of the shapes we can render. Returns
 * null for shapes we do not understand — never fabricate a stat.
 */
export function editPatchFromInput(
  input: Record<string, unknown> | null | undefined,
): EditPatch | null {
  if (!input) return null;

  const oldStr = str(input.old_string) ?? str(input.oldString) ?? str(input.old_str);
  const newStr = str(input.new_string) ?? str(input.newString) ?? str(input.new_str);
  if (oldStr !== null && newStr !== null) return { kind: "replace", oldStr, newStr };
  // `old_string` alone is a removal; `new_string` alone is an insertion.
  if (oldStr !== null) return { kind: "delete", content: oldStr };
  if (newStr !== null) return { kind: "create", content: newStr };

  const patchText = str(input.patch) ?? str(input.diff);
  if (patchText !== null) return { kind: "patch", text: patchText };

  const content = str(input.content) ?? str(input.contents) ?? str(input.text);
  if (content !== null) return { kind: "create", content };

  return null;
}

/**
 * Count `+`/`-` lines in unified-patch text. `+++`/`---` are file headers, not
 * content, and `\ No newline at end of file` is a marker.
 */
function patchStats(text: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

function computeEditStat(interaction: ToolInteraction): EditStat {
  const presentation = interaction.presentation;
  const raw = interaction.callEvent?.tool_input_json;
  const presented = presentation?.wrapper_recedes ? presentation.tool_input_json : raw;
  const input =
    presented && typeof presented === "object" && !Array.isArray(presented)
      ? (presented as Record<string, unknown>)
      : null;
  if (!input) return NO_STAT;

  const filePath = editFilePath(input);
  const patch = editPatchFromInput(input);
  if (!patch) {
    // Known file, unknown shape: still worth naming the file, without a stat.
    if (!filePath) return NO_STAT;
    return { ...NO_STAT, filePath, fileName: basename(filePath) };
  }

  const base = { filePath, fileName: basename(filePath), patch };

  if (patch.kind === "create") {
    const added = countLines(patch.content);
    return { ...base, added, removed: 0, hasStat: true };
  }
  if (patch.kind === "delete") {
    const removed = countLines(patch.content);
    return { ...base, added: 0, removed, hasStat: true };
  }
  if (patch.kind === "patch") {
    const { added, removed } = patchStats(patch.text);
    return { ...base, added, removed, hasStat: true };
  }

  // Replace: budget check *before* paying for the LCS table.
  const oldLines = countLines(patch.oldStr);
  const newLines = countLines(patch.newStr);
  if (oldLines * newLines > DIFF_CELL_BUDGET) {
    return { ...base, added: 0, removed: 0, hasStat: false };
  }

  let added = 0;
  let removed = 0;
  for (const line of lineDiff(patch.oldStr, patch.newStr)) {
    if (line.kind === "add") added += 1;
    else if (line.kind === "remove") removed += 1;
  }
  return { ...base, added, removed, hasStat: true };
}

const editStatCache = new WeakMap<ToolInteraction, EditStat>();

/** Memoized per interaction — consulted from render paths. */
export function getEditStat(interaction: ToolInteraction): EditStat {
  const cached = editStatCache.get(interaction);
  if (cached) return cached;
  const stat = computeEditStat(interaction);
  editStatCache.set(interaction, stat);
  return stat;
}

/** `timelineModel.ts +4 −1`, `timelineModel.ts` when no stat is available. */
export function formatEditStat(stat: EditStat): string | null {
  const name = stat.fileName;
  if (!name) return null;
  if (!stat.hasStat) return name;
  return `${name} +${stat.added} −${stat.removed}`;
}
