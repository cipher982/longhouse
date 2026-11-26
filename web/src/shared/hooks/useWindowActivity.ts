import { useSyncExternalStore } from "react";

export type WindowActivity = "focused" | "ambient" | "hidden";

/** Wait this long after window blur before dropping to ambient, so brief
 *  Cmd-Tab switches and short unfocused test runs never flicker. */
export const AMBIENT_BLUR_GRACE_MS = 10_000;

/** Hovering or scrolling an unfocused window (e.g. second monitor) wakes it
 *  for this long before returning to ambient. */
export const POINTER_WAKE_LINGER_MS = 20_000;

let currentActivity: WindowActivity = "focused";
let initialized = false;
let blurTimer: number | null = null;
let pointerWakeTimer: number | null = null;
const listeners = new Set<() => void>();

function syncDocumentAttribute(state: WindowActivity): void {
  if (typeof document === "undefined") return;
  if (document.documentElement.dataset.windowActivity !== state) {
    document.documentElement.dataset.windowActivity = state;
  }
}

function setActivity(next: WindowActivity): void {
  if (currentActivity === next) {
    syncDocumentAttribute(next);
    return;
  }
  currentActivity = next;
  syncDocumentAttribute(next);
  for (const listener of listeners) {
    listener();
  }
}

function clearTimers(): void {
  if (typeof window === "undefined") return;
  if (blurTimer !== null) {
    window.clearTimeout(blurTimer);
    blurTimer = null;
  }
  if (pointerWakeTimer !== null) {
    window.clearTimeout(pointerWakeTimer);
    pointerWakeTimer = null;
  }
}

function armAmbientAfter(delayMs: number): void {
  if (typeof window === "undefined") return;
  if (blurTimer !== null) {
    window.clearTimeout(blurTimer);
    blurTimer = null;
  }
  blurTimer = window.setTimeout(() => {
    blurTimer = null;
    if (typeof document === "undefined") return;
    if (document.hidden) {
      setActivity("hidden");
    } else if (!document.hasFocus()) {
      setActivity("ambient");
    }
  }, delayMs);
}

function handleVisibilityChange(): void {
  if (typeof document === "undefined") return;
  clearTimers();
  if (document.hidden) {
    setActivity("hidden");
    return;
  }
  setActivity("focused");
  if (!document.hasFocus()) {
    armAmbientAfter(AMBIENT_BLUR_GRACE_MS);
  }
}

function handleFocus(): void {
  if (typeof document === "undefined" || document.hidden) return;
  clearTimers();
  setActivity("focused");
}

function handleBlur(): void {
  if (typeof document === "undefined" || document.hidden) return;
  if (pointerWakeTimer !== null) {
    window.clearTimeout(pointerWakeTimer);
    pointerWakeTimer = null;
  }
  armAmbientAfter(AMBIENT_BLUR_GRACE_MS);
}

function handlePointerWake(): void {
  if (typeof document === "undefined" || document.hidden || document.hasFocus()) return;
  if (blurTimer !== null) {
    window.clearTimeout(blurTimer);
    blurTimer = null;
  }
  setActivity("focused");
  if (pointerWakeTimer !== null) {
    window.clearTimeout(pointerWakeTimer);
  }
  pointerWakeTimer = window.setTimeout(() => {
    pointerWakeTimer = null;
    if (typeof document === "undefined") return;
    if (document.hidden) {
      setActivity("hidden");
    } else if (!document.hasFocus()) {
      setActivity("ambient");
    }
  }, POINTER_WAKE_LINGER_MS);
}

function ensureInitialized(): void {
  if (initialized || typeof window === "undefined" || typeof document === "undefined") {
    return;
  }
  initialized = true;
  if (document.hidden) {
    currentActivity = "hidden";
    syncDocumentAttribute("hidden");
  } else {
    currentActivity = "focused";
    syncDocumentAttribute("focused");
    if (typeof document.hasFocus === "function" && !document.hasFocus()) {
      armAmbientAfter(AMBIENT_BLUR_GRACE_MS);
    }
  }
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("focus", handleFocus);
  window.addEventListener("blur", handleBlur);
  window.addEventListener("pointerenter", handlePointerWake, { passive: true });
  window.addEventListener("wheel", handlePointerWake, { passive: true });
}

export function getWindowActivity(): WindowActivity {
  ensureInitialized();
  return currentActivity;
}

export function subscribeWindowActivity(listener: () => void): () => void {
  ensureInitialized();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Three-tier browser window activity state:
 * - "focused": visible and actively focused (or recently hovered/scrolled on a second monitor)
 * - "ambient": visible (`document.hidden === false`) but unfocused past the grace window
 * - "hidden": tab hidden or window minimized (`document.hidden === true`)
 *
 * Also mirrors the active state onto `<html data-window-activity="...">` so CSS
 * can pause decorative infinite animations without React re-renders.
 */
export function useWindowActivity(): WindowActivity {
  return useSyncExternalStore(
    subscribeWindowActivity,
    getWindowActivity,
    () => "focused",
  );
}

/** Test-only reset for deterministic fake-timer unit tests. */
export function resetWindowActivityForTests(): void {
  clearTimers();
  if (initialized && typeof window !== "undefined" && typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    window.removeEventListener("focus", handleFocus);
    window.removeEventListener("blur", handleBlur);
    window.removeEventListener("pointerenter", handlePointerWake);
    window.removeEventListener("wheel", handlePointerWake);
  }
  initialized = false;
  currentActivity = "focused";
  if (typeof document !== "undefined") {
    delete document.documentElement.dataset.windowActivity;
  }
}
