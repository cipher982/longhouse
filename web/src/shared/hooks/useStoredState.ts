import { useCallback, useState } from "react";

/**
 * A per-device preference kept in localStorage. Storage can be missing or
 * throw (private windows, blocked site data), so every read and write is
 * guarded and the in-memory value still works without it. `parse` validates
 * what was stored; anything it rejects falls back to `initial`.
 */
export function useStoredState<T>(
  key: string,
  initial: T,
  parse: (raw: unknown) => T | null = (raw) => raw as T,
): [T, (next: T | ((previous: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => readStored(key, initial, parse));

  const update = useCallback(
    (next: T | ((previous: T) => T)) => {
      setValue((previous) => {
        const resolved = typeof next === "function" ? (next as (previous: T) => T)(previous) : next;
        try {
          window.localStorage.setItem(key, JSON.stringify(resolved));
        } catch {
          // Storage unavailable: keep the in-memory value for this tab.
        }
        return resolved;
      });
    },
    [key],
  );

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
