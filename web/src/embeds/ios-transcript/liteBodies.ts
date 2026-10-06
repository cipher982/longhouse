import { postToNative } from "./bridge";

/// Expanding a row whose bodies a lite page cut asks native for the full
/// ones. Native drops cursors it already holds or is loading, so a repeated
/// toggle (or the re-render restoring the row open) costs nothing.
export function attachToolBodyHandlers(scope: ParentNode = document): void {
  const rows = scope instanceof Element && scope.matches("details[data-body-cursors]")
    ? [scope]
    : Array.from(scope.querySelectorAll("details[data-body-cursors]"));
  for (const row of rows) {
    row.addEventListener("toggle", () => {
      if (!(row as HTMLDetailsElement).open) return;
      const cursors = (row.getAttribute("data-body-cursors") || "").split(" ").filter(Boolean);
      if (cursors.length) postToNative({ type: "loadToolBodies", cursors });
    });
  }
}
