/**
 * TraceIdDisplay - Shows trace_id for debugging
 *
 * Displays the current trace_id in a small footer badge.
 * Click to copy - use this ID with `make debug-trace TRACE=<id>` for debugging.
 *
 * The trace_id is:
 * - Set when a concierge course starts
 * - Cleared after a course completes (with fade delay)
 * - Persistent during course for easy copying
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { eventBus } from '../../lib/event-bus';

interface TraceIdDisplayProps {
  /** Show in dev mode only (default: true) */
  devOnly?: boolean;
}

export function TraceIdDisplay({ devOnly = true }: TraceIdDisplayProps): React.ReactElement | null {
  const [traceId, setTraceId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [fading, setFading] = useState(false);
  const fadeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Determine if we should render (hooks must run unconditionally)
  const isDev = import.meta.env.DEV;
  const shouldRender = !(devOnly && !isDev);

  useEffect(() => {
    // Subscribe to concierge events
    const unsubStarted = eventBus.on('concierge:started', (data) => {
      // Clear any pending fade timeout when a new course starts
      if (fadeTimeoutRef.current) {
        clearTimeout(fadeTimeoutRef.current);
        fadeTimeoutRef.current = null;
      }
      if (data.traceId) {
        setTraceId(data.traceId);
        setFading(false);
        setCopied(false);
      }
    });

    const unsubComplete = eventBus.on('concierge:complete', (data) => {
      // Keep trace_id visible for a moment after completion for easy copying
      // Then fade out (but keep traceId for debugging until next course)
      if (data.traceId) {
        setTraceId(data.traceId);
      }
      setFading(true);
      // Clear previous timeout if any
      if (fadeTimeoutRef.current) {
        clearTimeout(fadeTimeoutRef.current);
      }
      fadeTimeoutRef.current = setTimeout(() => {
        setFading(false);
        fadeTimeoutRef.current = null;
      }, 5000);
    });

    const unsubError = eventBus.on('concierge:error', (data) => {
      // Keep trace_id on errors - it's especially useful for debugging
      if (data.traceId) {
        setTraceId(data.traceId);
      }
    });

    const unsubCleared = eventBus.on('concierge:cleared', () => {
      if (fadeTimeoutRef.current) {
        clearTimeout(fadeTimeoutRef.current);
        fadeTimeoutRef.current = null;
      }
      setTraceId(null);
      setCopied(false);
      setFading(false);
    });

    return () => {
      // Cleanup: clear timeout and unsubscribe from events
      if (fadeTimeoutRef.current) {
        clearTimeout(fadeTimeoutRef.current);
      }
      unsubStarted();
      unsubComplete();
      unsubError();
      unsubCleared();
    };
  }, []);

  const handleCopy = useCallback(async () => {
    if (!traceId) return;

    try {
      await navigator.clipboard.writeText(traceId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      // Fallback for insecure contexts (e.g., E2E tests)
      console.warn('[TraceIdDisplay] Clipboard copy failed:', err);
    }
  }, [traceId]);

  if (!shouldRender || !traceId) {
    return null;
  }

  // Truncate for display (first 8 chars of UUID)
  const shortId = traceId.slice(0, 8);

  return (
    <div
      className={`trace-id-display ${fading ? 'trace-id-display--fading' : ''}`}
      data-testid="trace-id-display"
      data-trace-id={traceId}
    >
      <button
        className="trace-id-display__button"
        onClick={handleCopy}
        title={`Copy full trace ID: ${traceId}\n\nDebug with:\nmake debug-trace TRACE=${traceId}`}
      >
        <span className="trace-id-display__label">trace:</span>
        <code className="trace-id-display__id">{shortId}</code>
        <span className="trace-id-display__icon">{copied ? '✓' : '📋'}</span>
      </button>
    </div>
  );
}
