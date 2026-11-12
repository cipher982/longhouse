import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "longhouse.liveThinking.expanded";
const CHANGE_EVENT = "longhouse:live-thinking-pref";

/**
 * The key that toggles live thinking rows. A plain `t`: the existing session
 * shortcuts all use Ctrl or Meta (text size, switcher, new session) or Alt+digit
 * (rail), and no page handler binds an unmodified letter. Ctrl+O, which Claude's
 * own TUI uses, is the browser's Open File, so it is not taken. The key is
 * ignored with any modifier, inside a field, and under a modal.
 */
export const LIVE_THINKING_TOGGLE_KEY = "t";

function readStored(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function isFieldTarget(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    Boolean(target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])"))
  );
}

/** Whether live thinking rows are open: collapsed unless this viewer opened them, remembered per browser. */
export function useLiveThinkingExpanded(): [boolean, (next: boolean) => void] {
  const [expanded, setExpandedState] = useState<boolean>(readStored);

  const setExpanded = useCallback((next: boolean) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
    } catch {
      // Storage can be blocked; the choice then lasts until reload.
    }
    setExpandedState(next);
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: next }));
  }, []);

  useEffect(() => {
    const onChange = (event: Event) => setExpandedState(Boolean((event as CustomEvent<boolean>).detail));
    window.addEventListener(CHANGE_EVENT, onChange);
    return () => window.removeEventListener(CHANGE_EVENT, onChange);
  }, []);

  return [expanded, setExpanded];
}

/** Mount once per live thinking row set: the toggle key flips every row. */
export function useLiveThinkingHotkey(expanded: boolean, setExpanded: (next: boolean) => void): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== LIVE_THINKING_TOGGLE_KEY || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      if (event.repeat || isFieldTarget(event.target) || document.querySelector('[aria-modal="true"]')) return;
      setExpanded(!expanded);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded, setExpanded]);
}
