import { escapeHtml } from "./escape";
import type { DiffLine } from "./types";

/// Render diff lines as gutter-prefixed rows (R3). Mirrors EditDiffView.
export function renderDiff(lines: DiffLine[] | null | undefined): string {
  if (!lines || !lines.length) return "";
  const rows = lines
    .map((line) => {
      const gutter = line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " ";
      return (
        '<div class="diff-line diff-line--' +
        line.kind +
        '">' +
        '<span class="diff-gutter">' +
        gutter +
        "</span>" +
        '<span class="diff-text">' +
        escapeHtml(line.text || " ") +
        "</span></div>"
      );
    })
    .join("");
  return '<div class="section-label">Diff</div><div class="diff">' + rows + "</div>";
}
