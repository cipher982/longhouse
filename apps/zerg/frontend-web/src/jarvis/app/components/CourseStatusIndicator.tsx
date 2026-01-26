/**
 * CourseStatusIndicator - Tracks and displays concierge course status
 *
 * Listens to SSE events to track course lifecycle:
 * - idle: no active course
 * - running: concierge is processing
 * - complete: course finished successfully
 * - failed: course errored
 *
 * Exposes status via data-testid="course-status" for E2E testing.
 */

import { useState, useEffect } from 'react';
import { eventBus } from '../../lib/event-bus';

export type CourseStatus = 'idle' | 'running' | 'complete' | 'failed';

interface CourseStatusIndicatorProps {
  className?: string;
  /** Show visual indicator (default: false for hidden/test-only mode) */
  visible?: boolean;
}

export function CourseStatusIndicator({ className, visible = false }: CourseStatusIndicatorProps): React.ReactElement {
  const [status, setStatus] = useState<CourseStatus>('idle');
  const [courseId, setCourseId] = useState<number | null>(null);

  useEffect(() => {
    // Subscribe to concierge lifecycle events
    const unsubStarted = eventBus.on('concierge:started', (data) => {
      setStatus('running');
      setCourseId(data.courseId);
    });

    const unsubComplete = eventBus.on('concierge:complete', (data) => {
      setStatus('complete');
      // Keep courseId for reference
      if (data.courseId) {
        setCourseId(data.courseId);
      }
    });

    const unsubError = eventBus.on('concierge:error', () => {
      setStatus('failed');
    });

    const unsubDeferred = eventBus.on('concierge:deferred', () => {
      // Deferred is still considered "complete" from UI perspective
      // (work continues in background but UI interaction is done)
      setStatus('complete');
    });

    const unsubCleared = eventBus.on('concierge:cleared', () => {
      setStatus('idle');
      setCourseId(null);
    });

    return () => {
      unsubStarted();
      unsubComplete();
      unsubError();
      unsubDeferred();
      unsubCleared();
    };
  }, []);

  // Note: Status persists until next interaction or explicit clear via concierge:cleared event.
  // This ensures E2E tests can reliably check the status without race conditions.

  return (
    <div
      data-testid="course-status"
      data-course-status={status}
      data-course-id={courseId ?? ''}
      className={`course-status-indicator ${className || ''}`}
      style={{ display: visible ? 'block' : 'none' }}
    >
      {visible && (
        <span className={`course-status-indicator__badge course-status-indicator__badge--${status}`}>
          {status === 'idle' && 'Ready'}
          {status === 'running' && 'Processing...'}
          {status === 'complete' && 'Complete'}
          {status === 'failed' && 'Failed'}
        </span>
      )}
    </div>
  );
}
