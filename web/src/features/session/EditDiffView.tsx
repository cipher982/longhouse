import { collapseUnchanged, lineDiff, type DiffLine } from "@/shared/session/model/diff";
import type { EditStat } from "@/shared/session/model/editSummary";

/**
 * Render an edit as a diff. Covers every shape `getEditStat` understands:
 * replace runs a line diff, create/delete render as all-added/all-removed, and
 * patch text renders its own hunks. Stats come from the memoized stat so the
 * collapsed header and this body cannot disagree.
 */
export function EditDiffView({ stat }: { stat: EditStat }) {
  const patch = stat.patch;
  if (!patch) return null;

  const lines: DiffLine[] =
    patch.kind === "replace"
      ? collapseUnchanged(lineDiff(patch.oldStr, patch.newStr), 2)
      : patch.kind === "create"
        ? patch.content.split("\n").map((text, i) => ({ kind: "add" as const, text, oldLine: null, newLine: i + 1 }))
        : patch.kind === "delete"
          ? patch.content.split("\n").map((text, i) => ({ kind: "remove" as const, text, oldLine: i + 1, newLine: null }))
          : patch.text.split("\n").map((text) => ({
              kind:
                text.startsWith("+") && !text.startsWith("+++")
                  ? ("add" as const)
                  : text.startsWith("-") && !text.startsWith("---")
                    ? ("remove" as const)
                    : ("equal" as const),
              text,
              oldLine: null,
              newLine: null,
            }));

  return (
    <section className="tl-detail__block">
      <div className="tl-detail__label">
        diff
        {stat.filePath ? <span className="tl-detail__path"> · {stat.filePath}</span> : null}
        {stat.hasStat ? (
          <>
            <span className="tl-detail__diff-stat tl-detail__diff-stat--remove"> −{stat.removed}</span>
            <span className="tl-detail__diff-stat tl-detail__diff-stat--add"> +{stat.added}</span>
            <span className="sr-only">{`${stat.added} lines added, ${stat.removed} lines removed`}</span>
          </>
        ) : null}
      </div>
      <div className="tl-diff">
        {lines.map((line, i) => (
          <div key={i} className={`tl-diff__line tl-diff__line--${line.kind}`}>
            <span className="tl-diff__gutter">
              {line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " "}
            </span>
            <span className="tl-diff__text">{line.text || "\u00a0"}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
