/**
 * Minimal line-diff for rendering `old_string` → `new_string` pairs in the
 * timeline's Edit-tool detail. Not a general-purpose diff engine — scoped to
 * the small strings the `Edit`/`NotebookEdit`/`apply_patch` tools produce.
 *
 * Uses LCS (O(n*m) time, O(n*m) memory). Old/new are typically under a few
 * hundred lines, so this is fine. For truly large edits we could switch to
 * Myers later.
 */

export type DiffLine = {
  kind: "equal" | "add" | "remove";
  text: string;
  oldLine: number | null;
  newLine: number | null;
};

export function lineDiff(oldStr: string, newStr: string): DiffLine[] {
  const a = oldStr.split("\n");
  const b = newStr.split("\n");
  const m = a.length;
  const n = b.length;

  // LCS table
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  let oldNo = 1;
  let newNo = 1;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push({ kind: "equal", text: a[i], oldLine: oldNo++, newLine: newNo++ });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: "remove", text: a[i], oldLine: oldNo++, newLine: null });
      i++;
    } else {
      out.push({ kind: "add", text: b[j], oldLine: null, newLine: newNo++ });
      j++;
    }
  }
  while (i < m) out.push({ kind: "remove", text: a[i++], oldLine: oldNo++, newLine: null });
  while (j < n) out.push({ kind: "add", text: b[j++], oldLine: null, newLine: newNo++ });

  return out;
}

/**
 * Collapse long unchanged runs into a single "… N unchanged lines …" marker.
 * Keeps `context` lines on either side of each change so the diff reads
 * naturally. A `context` of 2 means: show 2 unchanged lines immediately
 * before and after any change block.
 */
export function collapseUnchanged(lines: DiffLine[], context = 2): DiffLine[] {
  if (lines.length <= context * 2 + 1) return lines;

  const keep = new Array<boolean>(lines.length).fill(false);
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].kind !== "equal") {
      for (let k = Math.max(0, i - context); k <= Math.min(lines.length - 1, i + context); k++) {
        keep[k] = true;
      }
    }
  }

  const out: DiffLine[] = [];
  let skipped = 0;
  for (let i = 0; i < lines.length; i++) {
    if (keep[i]) {
      if (skipped > 0) {
        out.push({ kind: "equal", text: `… ${skipped} unchanged line${skipped === 1 ? "" : "s"} …`, oldLine: null, newLine: null });
        skipped = 0;
      }
      out.push(lines[i]);
    } else {
      skipped += 1;
    }
  }
  if (skipped > 0) {
    out.push({ kind: "equal", text: `… ${skipped} unchanged line${skipped === 1 ? "" : "s"} …`, oldLine: null, newLine: null });
  }
  return out;
}
