/**
 * Lightweight module-level pub/sub for API health state.
 *
 * Pages call reportApiError / clearApiError; the StatusFooter subscribes via
 * useApiHealth() to reflect degraded state without requiring Context threading.
 */
import { useSyncExternalStore } from "react";

type Listener = (error: Error | null) => void;

let _currentError: Error | null = null;
const _listeners = new Set<Listener>();

function subscribe(listener: Listener): () => void {
  _listeners.add(listener);
  return () => {
    _listeners.delete(listener);
  };
}

function getSnapshot(): Error | null {
  return _currentError;
}

export function reportApiError(error: Error): void {
  _currentError = error;
  _listeners.forEach((l) => l(error));
}

export function clearApiError(): void {
  if (_currentError === null) return; // no-op if already clear
  _currentError = null;
  _listeners.forEach((l) => l(null));
}

export function useApiHealth(): Error | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
