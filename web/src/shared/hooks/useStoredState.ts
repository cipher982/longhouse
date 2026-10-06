import { useCallback, useEffect, useRef, useState } from "react";

/**
 * A per-device preference kept in localStorage. Storage can be missing or
 * throw (private windows, blocked site data), so every read and write is
 * guarded and the in-memory value still works without it. `parse` validates
 * what was stored; anything it rejects falls back to `initial`. Nothing is
 * written until the user changes the value, so a later change to `initial`
 * still reaches people who never chose.
 */
export function useStoredState<T>(
  key: string,
  initial: T,
  parse: (raw: unknown) => T | null = (raw) => raw as T,
): [T, (next: T | ((previous: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => readStored(key, initial, parse));
  const changedRef = useRef(false);

  const update = useCallback((next: T | ((previous: T) => T)) => {
    changedRef.current = true;
    setValue(next);
  }, []);

  useEffect(() => {
    if (!changedRef.current) return;
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage unavailable: keep the in-memory value for this tab.
    }
  }, [key, value]);

  return [value, update];
}

export function readStored<T>(key: string, initial: T, parse: (raw: unknown) => T | null): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw == null) return initial;
    return parse(JSON.parse(raw)) ?? initial;
  } catch {
    return initial;
  }
}
