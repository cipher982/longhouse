/**
 * Lightweight module-level pub/sub for API health state.
 *
 * Pages call reportApiError / clearApiError; the StatusFooter subscribes via
 * useApiHealth() to reflect degraded state without requiring Context threading.
 */
import { useState, useEffect } from "react";

type Listener = (error: Error | null) => void;

let _currentError: Error | null = null;
const _listeners = new Set<Listener>();

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
  const [error, setError] = useState<Error | null>(_currentError);
  useEffect(() => {
    _listeners.add(setError);
    return () => {
      _listeners.delete(setError);
    };
  }, []);
  return error;
}
