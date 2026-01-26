/**
 * TimelineLogger - Clean timeline view for chat observability
 *
 * Listens to EventBus events and outputs a condensed timing view.
 * Enabled via URL param: ?timeline=true
 *
 * Timeline format:
 * [Timeline] correlationId=abc123
 *   T+0ms      send              Message dispatched
 *   T+45ms     backend_received  course_id=1
 *   T+120ms    concierge_started
 *   T+850ms    commis_spawned    job_id=1
 *   T+1200ms   commis_started    commis_id=xyz
 *   T+1500ms   tool_started      ssh_exec
 *   T+2100ms   tool_completed    ssh_exec (600ms)
 *   T+2800ms   commis_complete   (1600ms total)
 *   T+3200ms   concierge_complete (3155ms total)
 */

import { eventBus } from './event-bus';

interface TimelineEvent {
  phase: string;
  timestamp: number;
  metadata?: Record<string, unknown>;
}

export class TimelineLogger {
  private enabled: boolean = false;
  private events: TimelineEvent[] = [];
  private startTime: number | null = null;
  private currentMessageId: string | null = null;
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

    // Concierge lifecycle events
    this.unsubscribers.push(
      eventBus.on('concierge:started', (data) => {
        this.recordEvent('concierge_started', data.timestamp, { courseId: data.courseId, task: data.task });
      })
    );

    this.unsubscribers.push(
      eventBus.on('concierge:thinking', (data) => {
        this.recordEvent('concierge_thinking', data.timestamp, { message: data.message });
      })
    );

    this.unsubscribers.push(
      eventBus.on('concierge:complete', (data) => {
        this.recordEvent('concierge_complete', data.timestamp, {
          courseId: data.courseId,
          status: data.status,
          durationMs: data.durationMs,
        });
        // Output timeline on completion
        this.outputTimeline();
      })
    );

    this.unsubscribers.push(
      eventBus.on('concierge:error', (data) => {
        this.recordEvent('concierge_error', data.timestamp, { message: data.message });
        this.outputTimeline();
      })
    );

    // Commis lifecycle events
    this.unsubscribers.push(
      eventBus.on('concierge:commis_spawned', (data) => {
        this.recordEvent('commis_spawned', data.timestamp, { jobId: data.jobId, task: data.task });
      })
    );

    this.unsubscribers.push(
      eventBus.on('concierge:commis_started', (data) => {
        this.recordEvent('commis_started', data.timestamp, { jobId: data.jobId, commisId: data.commisId });
      })
    );

    this.unsubscribers.push(
      eventBus.on('concierge:commis_complete', (data) => {
        this.recordEvent('commis_complete', data.timestamp, {
          jobId: data.jobId,
          commisId: data.commisId,
          status: data.status,
          durationMs: data.durationMs,
        });
      })
    );

    // Commis tool events
    this.unsubscribers.push(
      eventBus.on('commis:tool_started', (data) => {
        this.recordEvent('tool_started', data.timestamp, {
          commisId: data.commisId,
          toolName: data.toolName,
          toolCallId: data.toolCallId,
        });
      })
    );

    this.unsubscribers.push(
      eventBus.on('commis:tool_completed', (data) => {
        this.recordEvent('tool_completed', data.timestamp, {
          commisId: data.commisId,
          toolName: data.toolName,
          toolCallId: data.toolCallId,
          durationMs: data.durationMs,
        });
      })
    );

    this.unsubscribers.push(
      eventBus.on('commis:tool_failed', (data) => {
        this.recordEvent('tool_failed', data.timestamp, {
          commisId: data.commisId,
          toolName: data.toolName,
          toolCallId: data.toolCallId,
          durationMs: data.durationMs,
          error: data.error,
        });
      })
    );

    // Deferred event
    this.unsubscribers.push(
      eventBus.on('concierge:deferred', (data) => {
        this.recordEvent('concierge_deferred', data.timestamp, { courseId: data.courseId, message: data.message });
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

    const messageId = this.currentMessageId || 'unknown';
    const lines: string[] = [];

    for (const event of this.events) {
      const offsetMs = this.startTime !== null ? event.timestamp - this.startTime : 0;
      const offsetStr = `T+${offsetMs}ms`.padEnd(12);
      const phaseStr = event.phase.padEnd(20);

      // Format metadata for display
      let metadataStr = '';
      if (event.metadata) {
        const parts: string[] = [];
        if (event.metadata.courseId) parts.push(`course_id=${event.metadata.courseId}`);
        if (event.metadata.jobId) parts.push(`job_id=${event.metadata.jobId}`);
        if (event.metadata.commisId) parts.push(`commis_id=${event.metadata.commisId}`);
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
    console.groupCollapsed(`[Timeline] ${messageId} (${this.events.length} events)`);
    console.log(lines.join('\n'));
    console.groupEnd();

    // Also output plain log lines for E2E test capture (console.groupCollapsed isn't
    // reliably captured by Playwright's page.on('console'))
    console.log(`[Timeline] messageId=${messageId}`);
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
    this.currentMessageId = null;
  }

  /**
   * Set message ID for current timeline
   */
  setMessageId(messageId: string): void {
    this.currentMessageId = messageId;
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
