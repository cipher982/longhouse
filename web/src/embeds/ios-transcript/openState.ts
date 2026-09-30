/// A re-render rebuilds every disclosure, so remember which ones the user
/// opened. A disclosure opts in with `data-open-key` (its item id).
export function captureOpenKeys(root: ParentNode): Set<string> {
  return new Set(
    Array.from(root.querySelectorAll("details[data-open-key][open]"))
      .map((node) => node.getAttribute("data-open-key"))
      .filter((key): key is string => Boolean(key)),
  );
}

export function restoreOpenKeys(root: ParentNode, keys: Set<string>): void {
  if (!keys.size) return;
  root.querySelectorAll<HTMLDetailsElement>("details[data-open-key]").forEach((node) => {
    if (keys.has(node.getAttribute("data-open-key") ?? "")) node.open = true;
  });
}
