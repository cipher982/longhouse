import type { HTMLAttributes, KeyboardEvent, MouseEvent, ReactNode } from "react";

/**
 * The clickable head of a transcript row. It acts as a button (role, focus,
 * Enter and Space) but is not a <button>, because a <button> cannot hold a
 * text selection: press-and-drag over a command or a purpose highlights it,
 * and only a click that selected nothing expands the row.
 */
export function RowHead({
  onActivate,
  children,
  ...rest
}: Omit<HTMLAttributes<HTMLDivElement>, "onClick" | "role" | "tabIndex"> & {
  onActivate: () => void;
  children: ReactNode;
}) {
  const handleClick = (event: MouseEvent<HTMLDivElement>) => {
    const selection = typeof window !== "undefined" ? window.getSelection() : null;
    // A press that selected text in this head (from either end) is a
    // selection, not a click.
    if (selection && !selection.isCollapsed && selection.toString().trim()) {
      const head = event.currentTarget;
      const touches = [selection.anchorNode, selection.focusNode].some((node) => node && head.contains(node));
      if (touches) return;
    }
    onActivate();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    rest.onKeyDown?.(event);
    if (event.defaultPrevented || event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      // Holding the key auto-repeats keydown; a button toggles once.
      if (!event.repeat) onActivate();
    }
  };
  return (
    <div {...rest} role="button" tabIndex={0} onClick={handleClick} onKeyDown={handleKeyDown}>
      {children}
    </div>
  );
}

/**
 * The literal target (command, path, URL) beside an authored purpose, dim and
 * on the same line, filling the row's empty middle instead of hiding on
 * hover. Hidden on narrow screens, where the purpose needs the width.
 */
export function TargetAside({ target, text }: { target: string | null | undefined; text: string | null | undefined }) {
  const line = target?.split("\n").map((part) => part.trim()).find(Boolean) ?? "";
  if (!line || line === text?.trim()) return null;
  return (
    <span className="tl-row__target" title={target ?? undefined} data-testid="row-target">
      {line}
    </span>
  );
}
