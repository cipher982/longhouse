/**
 * TimelineLogger - Clean timeline view for chat observability
 *
 * Listens to EventBus events and outputs a condensed timing view.
 * Enabled via URL param: ?timeline=true
 *
 * Timeline format:
 * [Timeline] correlationId=abc123
 *   T+0ms      send              Message dispatched
 *   T+45ms     backend_received  run_id=1
 *   T+120ms    supervisor_started
 *   T+850ms    worker_spawned    job_id=1
 *   T+1200ms   worker_started    worker_id=xyz
 *   T+1500ms   tool_started      ssh_exec
 *   T+2100ms   tool_completed    ssh_exec (600ms)
 *   T+2800ms   worker_complete   (1600ms total)
 *   T+3200ms   supervisor_complete (3155ms total)
 */

import { eventBus, type EventMap } from './event-bus';
import { logger } from '../core';

interface TimelineEvent {
  phase: string;
  timestamp: number;
  metadata?: Record<string, unknown>;
}

export class TimelineLogger {
  private enabled: boolean = false;
  private events: TimelineEvent[] = [];
  private startTime: number | null = null;
  private currentCorrelationId: string | null = null;
  private unsubscribers: Array<() => void> = [];

  constructor() {
    // Check URL param ?timeline=true or ?log=timeline
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      this.enabled = params.get('timeline') === 'true' || params.get('log') === 'timeline';

      if (this.enabled) {
        console.log('[TimelineLogger] Timeline mode enabled');
        this.setupListeners();
      }
    }
  }

  /**
   * Set up event listeners for timeline tracking
   */
  private setupListeners(): void {
    // Text channel: message sent (T+0)
    this.unsubscribers.push(
      eventBus.on('text_channel:sent', (data) => {
        this.recordEvent('send', data.timestamp, { text: data.text });
      })
    );

    // Supervisor lifecycle events
    this.unsubscribers.push(
      eventBus.on('supervisor:started', (data) => {
        this.recordEvent('supervisor_started', data.timestamp, { runId: data.runId, task: data.task });
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:thinking', (data) => {
        this.recordEvent('supervisor_thinking', data.timestamp, { message: data.message });
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:complete', (data) => {
        this.recordEvent('supervisor_complete', data.timestamp, {
          runId: data.runId,
          status: data.status,
          durationMs: data.durationMs,
        });
        // Output timeline on completion
        this.outputTimeline();
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:error', (data) => {
        this.recordEvent('supervisor_error', data.timestamp, { message: data.message });
        this.outputTimeline();
      })
    );

    // Worker lifecycle events
    this.unsubscribers.push(
      eventBus.on('supervisor:worker_spawned', (data) => {
        this.recordEvent('worker_spawned', data.timestamp, { jobId: data.jobId, task: data.task });
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:worker_started', (data) => {
        this.recordEvent('worker_started', data.timestamp, { jobId: data.jobId, workerId: data.workerId });
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:worker_complete', (data) => {
        this.recordEvent('worker_complete', data.timestamp, {
          jobId: data.jobId,
          workerId: data.workerId,
          status: data.status,
          durationMs: data.durationMs,
        });
      })
    );

    // Worker tool events
    this.unsubscribers.push(
      eventBus.on('worker:tool_started', (data) => {
        this.recordEvent('tool_started', data.timestamp, {
          workerId: data.workerId,
          toolName: data.toolName,
          toolCallId: data.toolCallId,
        });
      })
    );

    this.unsubscribers.push(
      eventBus.on('worker:tool_completed', (data) => {
        this.recordEvent('tool_completed', data.timestamp, {
          workerId: data.workerId,
          toolName: data.toolName,
          toolCallId: data.toolCallId,
          durationMs: data.durationMs,
        });
      })
    );

    this.unsubscribers.push(
      eventBus.on('worker:tool_failed', (data) => {
        this.recordEvent('tool_failed', data.timestamp, {
          workerId: data.workerId,
          toolName: data.toolName,
          toolCallId: data.toolCallId,
          durationMs: data.durationMs,
          error: data.error,
        });
      })
    );

    // Deferred event
    this.unsubscribers.push(
      eventBus.on('supervisor:deferred', (data) => {
        this.recordEvent('supervisor_deferred', data.timestamp, { runId: data.runId, message: data.message });
        this.outputTimeline();
      })
    );
  }

  /**
   * Record a timeline event
   */
  private recordEvent(phase: string, timestamp: number, metadata?: Record<string, unknown>): void {
    if (!this.enabled) return;

    // Initialize start time on first event
    if (this.startTime === null) {
      this.startTime = timestamp;
    }

    this.events.push({
      phase,
      timestamp,
      metadata,
    });
  }

  /**
   * Output the timeline to console
   */
  private outputTimeline(): void {
    if (!this.enabled || this.events.length === 0) return;

    const correlationId = this.currentCorrelationId || 'unknown';
    const lines: string[] = [];

    for (const event of this.events) {
      const offsetMs = this.startTime !== null ? event.timestamp - this.startTime : 0;
      const offsetStr = `T+${offsetMs}ms`.padEnd(12);
      const phaseStr = event.phase.padEnd(20);

      // Format metadata for display
      let metadataStr = '';
      if (event.metadata) {
        const parts: string[] = [];
        if (event.metadata.runId) parts.push(`run_id=${event.metadata.runId}`);
        if (event.metadata.jobId) parts.push(`job_id=${event.metadata.jobId}`);
        if (event.metadata.workerId) parts.push(`worker_id=${event.metadata.workerId}`);
        if (event.metadata.toolName) parts.push(`${event.metadata.toolName}`);
        if (event.metadata.durationMs) parts.push(`(${event.metadata.durationMs}ms)`);
        if (event.metadata.status) parts.push(`status=${event.metadata.status}`);
        if (event.metadata.message) {
          const msg = String(event.metadata.message);
          parts.push(msg.length > 40 ? msg.substring(0, 40) + '...' : msg);
        }
        metadataStr = parts.join(' ');
      }

      lines.push(`  ${offsetStr} ${phaseStr} ${metadataStr}`);
    }

    // Output as grouped console log for dev readability
    console.groupCollapsed(`[Timeline] ${correlationId} (${this.events.length} events)`);
    console.log(lines.join('\n'));
    console.groupEnd();

    // Also output plain log lines for E2E test capture (console.groupCollapsed isn't
    // reliably captured by Playwright's page.on('console'))
    console.log(`[Timeline] correlationId=${correlationId}`);
    for (const line of lines) {
      console.log(line);
    }

    // Reset for next message
    this.reset();
  }

  /**
   * Reset timeline state for next message
   */
  private reset(): void {
    this.events = [];
    this.startTime = null;
    this.currentCorrelationId = null;
  }

  /**
   * Set correlation ID for current timeline
   */
  setCorrelationId(correlationId: string): void {
    this.currentCorrelationId = correlationId;
  }

  /**
   * Clean up listeners
   */
  dispose(): void {
    this.unsubscribers.forEach((unsub) => unsub());
    this.unsubscribers = [];
  }
}

// Export singleton instance
export const timelineLogger = new TimelineLogger();
