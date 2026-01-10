/**
 * RunStatusIndicator - Tracks and displays supervisor run status
 *
 * Listens to SSE events to track run lifecycle:
 * - idle: no active run
 * - running: supervisor is processing
 * - complete: run finished successfully
 * - failed: run errored
 *
 * Exposes status via data-testid="run-status" for E2E testing.
 */

import { useState, useEffect } from 'react';
import { eventBus } from '../../lib/event-bus';

export type RunStatus = 'idle' | 'running' | 'complete' | 'failed';

interface RunStatusIndicatorProps {
  className?: string;
  /** Show visual indicator (default: false for hidden/test-only mode) */
  visible?: boolean;
}

export function RunStatusIndicator({ className, visible = false }: RunStatusIndicatorProps): React.ReactElement {
  const [status, setStatus] = useState<RunStatus>('idle');
  const [runId, setRunId] = useState<number | null>(null);

  useEffect(() => {
    // Subscribe to supervisor lifecycle events
    const unsubStarted = eventBus.on('supervisor:started', (data) => {
      setStatus('running');
      setRunId(data.runId);
    });

    const unsubComplete = eventBus.on('supervisor:complete', (data) => {
      setStatus('complete');
      // Keep runId for reference
      if (data.runId) {
        setRunId(data.runId);
      }
    });

    const unsubError = eventBus.on('supervisor:error', () => {
      setStatus('failed');
    });

    const unsubDeferred = eventBus.on('supervisor:deferred', () => {
      // Deferred is still considered "complete" from UI perspective
      // (work continues in background but UI interaction is done)
      setStatus('complete');
    });

    const unsubCleared = eventBus.on('supervisor:cleared', () => {
      setStatus('idle');
      setRunId(null);
    });

    return () => {
      unsubStarted();
      unsubComplete();
      unsubError();
      unsubDeferred();
      unsubCleared();
    };
  }, []);

  // Note: Status persists until next interaction or explicit clear via supervisor:cleared event.
  // This ensures E2E tests can reliably check the status without race conditions.

  return (
    <div
      data-testid="run-status"
      data-run-status={status}
      data-run-id={runId ?? ''}
      className={`run-status-indicator ${className || ''}`}
      style={{ display: visible ? 'block' : 'none' }}
    >
      {visible && (
        <span className={`run-status-indicator__badge run-status-indicator__badge--${status}`}>
          {status === 'idle' && 'Ready'}
          {status === 'running' && 'Processing...'}
          {status === 'complete' && 'Complete'}
          {status === 'failed' && 'Failed'}
        </span>
      )}
    </div>
  );
}
