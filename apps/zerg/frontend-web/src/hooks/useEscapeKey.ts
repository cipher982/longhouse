import { useEffect } from "react";
import { useLatest } from "./useLatest";

export function useEscapeKey(onEscape: (event: KeyboardEvent) => void, enabled = true) {
  const latestOnEscape = useLatest(onEscape);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        latestOnEscape.current(event);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [enabled, latestOnEscape]);
}
