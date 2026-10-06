import { useCallback, useEffect } from "react";
import { useStoredState } from "@/shared/hooks/useStoredState";
import {
  DEFAULT_DISPLAY_SETTINGS,
  DISPLAY_SETTINGS_STORAGE_KEY,
  parseDisplaySettings,
  stepTextSize,
  type DisplaySettings,
} from "./displaySettings";

/**
 * The transcript display settings, kept per device, plus ⌘+ / ⌘− (Ctrl on
 * other platforms) to step the text size. Those keys would otherwise zoom
 * the whole page; on the session view they size the transcript instead, and
 * ⌘0 still resets the browser zoom.
 */
export function useDisplaySettings() {
  const [settings, setSettings] = useStoredState<DisplaySettings>(
    DISPLAY_SETTINGS_STORAGE_KEY,
    DEFAULT_DISPLAY_SETTINGS,
    parseDisplaySettings,
  );

  const update = useCallback(
    (patch: Partial<DisplaySettings>) => setSettings((previous) => ({ ...previous, ...patch })),
    [setSettings],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      // A modal over the session (resume, launch, the switcher) keeps the
      // browser's own zoom.
      if (document.querySelector('[aria-modal="true"]')) return;
      const direction = event.key === "=" || event.key === "+" ? 1 : event.key === "-" ? -1 : 0;
      if (direction === 0) return;
      event.preventDefault();
      setSettings((previous) => ({ ...previous, size: stepTextSize(previous.size, direction) }));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [setSettings]);

  return { settings, update, replace: setSettings };
}
