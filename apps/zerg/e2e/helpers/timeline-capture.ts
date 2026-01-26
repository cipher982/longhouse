/**
 * TimelineCapture - Helper for capturing timeline events during E2E tests
 *
 * Intercepts SSE events from Jarvis chat to build timeline data structure
 * similar to frontend TimelineLogger. Exports metrics to JSON for analysis.
 *
 * Usage:
 *   const timeline = new TimelineCapture(page);
 *   await timeline.start();
 *   // ... perform chat interaction ...
 *   const events = await timeline.stop();
 *   await timeline.exportMetrics('test-name');
 */

import { type Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// ESM equivalent of __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

interface CapturedEvent {
  timestamp: number;
  phase: string;
  metadata?: Record<string, unknown>;
}

interface TimelineData {
  correlationId: string | null;
  courseId: number | null;
  events: CapturedEvent[];
  startTime: number;
  endTime: number;
  totalDurationMs: number;
  phases: Record<string, { offsetMs: number; timestamp: number; metadata?: Record<string, unknown> }>;
}

interface TimelineMetrics {
  testName: string;
  timestamp: string;
  timeline: TimelineData;
  summary: {
    totalDurationMs: number;
    conciergeThinkingMs?: number;
    commisExecutionMs?: number;
    toolExecutionMs?: number;
  };
}

export class TimelineCapture {
  private page: Page;
  private events: CapturedEvent[] = [];
  private startTime: number | null = null;
  private correlationId: string | null = null;
  private courseId: number | null = null;
  private capturing: boolean = false;

  constructor(page: Page) {
    this.page = page;
  }

  /**
   * Start capturing timeline events
   */
  async start(): Promise<void> {
    if (this.capturing) {
      throw new Error('Timeline capture already started');
    }

    this.capturing = true;
    this.events = [];
    this.startTime = Date.now();
    this.correlationId = null;
    this.courseId = null;

    // Inject event capture script into page context
    await this.page.addInitScript(() => {
      // Store captured events in window
      (window as any).__timelineEvents = [];

      // Intercept EventSource to capture SSE events
      const OriginalEventSource = (window as any).EventSource;
      if (OriginalEventSource) {
        (window as any).EventSource = function (url: string, config?: any) {
          const es = new OriginalEventSource(url, config);

          // Capture all messages
          es.addEventListener('message', (e: MessageEvent) => {
            try {
              const data = JSON.parse(e.data);
              (window as any).__timelineEvents.push({
                timestamp: Date.now(),
                phase: data.event_type || 'unknown',
                data: data,
              });
            } catch (err) {
              // Ignore parse errors
            }
          });

          // Capture custom event types
          const originalAddEventListener = es.addEventListener.bind(es);
          es.addEventListener = function (type: string, listener: any, options?: any) {
            if (type !== 'message' && type !== 'error' && type !== 'open') {
              const wrappedListener = (e: MessageEvent) => {
                try {
                  const data = JSON.parse(e.data);
                  (window as any).__timelineEvents.push({
                    timestamp: Date.now(),
                    phase: type,
                    data: data,
                  });
                } catch (err) {
                  // Ignore
                }
                listener(e);
              };
              return originalAddEventListener(type, wrappedListener, options);
            }
            return originalAddEventListener(type, listener, options);
          };

          return es;
        };
      }
    });
  }

  /**
   * Stop capturing and return timeline data
   */
  async stop(): Promise<TimelineData> {
    if (!this.capturing) {
      throw new Error('Timeline capture not started');
    }

    this.capturing = false;

    // Retrieve captured events from page context
    const capturedEvents = await this.page.evaluate(() => {
      return (window as any).__timelineEvents || [];
    });

    // Process events into timeline structure
    if (capturedEvents.length === 0) {
      return {
        correlationId: this.correlationId,
        courseId: this.courseId,
        events: [],
        startTime: this.startTime || Date.now(),
        endTime: Date.now(),
        totalDurationMs: 0,
        phases: {},
      };
    }

    // Extract correlation ID and course ID from first event
    for (const evt of capturedEvents) {
      if (evt.data?.client_correlation_id && !this.correlationId) {
        this.correlationId = evt.data.client_correlation_id;
      }
      if (evt.data?.payload?.course_id && !this.courseId) {
        this.courseId = evt.data.payload.course_id;
      }
      if (this.correlationId && this.courseId) break;
    }

    // Build timeline events
    const firstTimestamp = capturedEvents[0].timestamp;
    const lastTimestamp = capturedEvents[capturedEvents.length - 1].timestamp;

    for (const evt of capturedEvents) {
      const phase = evt.data?.event_type || evt.phase || 'unknown';
      const metadata = evt.data?.payload || {};

      this.events.push({
        timestamp: evt.timestamp,
        phase,
        metadata,
      });
    }

    // Build phases map (first occurrence of each phase)
    const phases: Record<string, { offsetMs: number; timestamp: number; metadata?: Record<string, unknown> }> = {};
    for (const event of this.events) {
      if (!phases[event.phase]) {
        phases[event.phase] = {
          offsetMs: event.timestamp - firstTimestamp,
          timestamp: event.timestamp,
          metadata: event.metadata,
        };
      }
    }

    return {
      correlationId: this.correlationId,
      courseId: this.courseId,
      events: this.events,
      startTime: firstTimestamp,
      endTime: lastTimestamp,
      totalDurationMs: lastTimestamp - firstTimestamp,
      phases,
    };
  }

  /**
   * Export metrics to JSON file
   */
  async exportMetrics(testName: string): Promise<string> {
    const timelineData = await this.stop();

    // Calculate summary statistics
    const summary = this.calculateSummary(timelineData);

    const metrics: TimelineMetrics = {
      testName,
      timestamp: new Date().toISOString(),
      timeline: timelineData,
      summary,
    };

    // Write to metrics directory
    const metricsDir = path.join(__dirname, '..', 'metrics');
    if (!fs.existsSync(metricsDir)) {
      fs.mkdirSync(metricsDir, { recursive: true });
    }

    const filename = `${testName}-${Date.now()}.json`;
    const filepath = path.join(metricsDir, filename);

    fs.writeFileSync(filepath, JSON.stringify(metrics, null, 2));

    return filepath;
  }

  /**
   * Calculate summary statistics from timeline data
   */
  private calculateSummary(timeline: TimelineData): {
    totalDurationMs: number;
    conciergeThinkingMs?: number;
    commisExecutionMs?: number;
    toolExecutionMs?: number;
  } {
    const { phases, totalDurationMs } = timeline;

    // Calculate concierge thinking time (concierge_started → commis_spawned)
    let conciergeThinkingMs: number | undefined = undefined;
    if (phases.concierge_started && phases.commis_spawned) {
      conciergeThinkingMs = phases.commis_spawned.offsetMs - phases.concierge_started.offsetMs;
    }

    // Calculate commis execution time (commis_spawned → commis_complete)
    let commisExecutionMs: number | undefined = undefined;
    if (phases.commis_spawned && phases.commis_complete) {
      commisExecutionMs = phases.commis_complete.offsetMs - phases.commis_spawned.offsetMs;
    }

    // Calculate tool execution time (first tool_started → last tool_completed/tool_failed)
    let toolExecutionMs: number | undefined = undefined;
    if (phases.tool_started) {
      const lastToolEvent = phases.tool_completed || phases.tool_failed;
      if (lastToolEvent) {
        toolExecutionMs = lastToolEvent.offsetMs - phases.tool_started.offsetMs;
      }
    }

    return {
      totalDurationMs,
      conciergeThinkingMs,
      commisExecutionMs,
      toolExecutionMs,
    };
  }

  /**
   * Get timeline data without stopping capture
   */
  async getEvents(): Promise<CapturedEvent[]> {
    return this.events;
  }

  /**
   * Get correlation ID
   */
  getCorrelationId(): string | null {
    return this.correlationId;
  }

  /**
   * Get course ID
   */
  getCourseId(): number | null {
    return this.courseId;
  }
}
