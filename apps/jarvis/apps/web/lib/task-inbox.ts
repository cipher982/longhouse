/**
 * Task Inbox Component for Jarvis
 *
 * Displays recent agent runs from Zerg backend in real-time.
 * Receives updates via SSE stream and shows run status, summaries, and timing.
 */

import { getJarvisClient, JarvisRunSummary, JarvisEventData } from '@jarvis/core';

export interface TaskInboxOptions {
  apiURL: string;
  onError?: (error: Error) => void;
  onRunUpdate?: (run: JarvisRunSummary) => void;
}

export class TaskInbox {
  private client;
  private container: HTMLElement | null = null;
  private runs: Map<number, JarvisRunSummary> = new Map();
  private isConnected = false;

  constructor(private options: TaskInboxOptions) {
    this.client = getJarvisClient(options.apiURL);
  }

  /**
   * Initialize the task inbox and start listening for updates
   */
  async initialize(): Promise<void> {
    try {
      // SaaS model: Jarvis uses the same login as the dashboard (zerg_jwt bearer token).
      // If not logged in, API calls will return 401 and surface via onError.

      // Load initial run history
      const recentRuns = await this.client.listRuns({ limit: 20 });
      recentRuns.forEach(run => this.runs.set(run.id, run));

      // Connect to SSE stream for real-time updates
      this.client.connectEventStream({
        onConnected: () => {
          this.isConnected = true;
          console.log('Task Inbox: Connected to Zerg SSE stream');
        },
        onRunCreated: (event: JarvisEventData) => {
          this.handleRunCreated(event);
        },
        onRunUpdated: (event: JarvisEventData) => {
          this.handleRunUpdated(event);
        },
        onError: (error) => {
          this.isConnected = false;
          console.error('Task Inbox: SSE error', error);
          this.options.onError?.(new Error('SSE connection failed'));
          // Auto-reconnect after 5 seconds
          setTimeout(() => this.reconnect(), 5000);
        },
      });

      this.render();
    } catch (error) {
      console.error('Task Inbox initialization failed:', error);
      this.options.onError?.(error as Error);
      throw error;
    }
  }

  /**
   * Handle new run created event
   */
  private handleRunCreated(event: JarvisEventData): void {
    const runId = event.payload.run_id as number;
    if (runId) {
      // Fetch full run details
      this.client.listRuns({ limit: 1 }).then(runs => {
        const newRun = runs.find(r => r.id === runId);
        if (newRun) {
          this.runs.set(runId, newRun);
          this.render();
        }
      });
    }
  }

  /**
   * Handle run status update event
   */
  private handleRunUpdated(event: JarvisEventData): void {
    const runId = event.payload.run_id as number;
    const existingRun = this.runs.get(runId);

    if (existingRun) {
      // Update existing run with new data
      const updatedRun: JarvisRunSummary = {
        ...existingRun,
        status: event.payload.status || existingRun.status,
        summary: event.payload.summary || existingRun.summary,
        updated_at: event.timestamp || new Date().toISOString(),
      };
      this.runs.set(runId, updatedRun);
      this.render();

      // Notify callback
      this.options.onRunUpdate?.(updatedRun);
    }
  }

  /**
   * Reconnect to SSE stream
   */
  private async reconnect(): Promise<void> {
    if (this.isConnected) return; // Already connected

    try {
      await this.initialize();
    } catch (error) {
      console.error('Task Inbox: Reconnection failed', error);
      // Will retry again via onError callback
    }
  }

  /**
   * Render the task inbox UI
   */
  render(container?: HTMLElement): void {
    if (container) {
      this.container = container;
    }

    if (!this.container) {
      console.warn('Task Inbox: No container element specified');
      return;
    }

    const sortedRuns = Array.from(this.runs.values())
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());

    const html = `
      <div class="task-inbox">
        <div class="task-inbox-header">
          <h3>Task Inbox</h3>
          <span class="connection-status ${this.isConnected ? 'connected' : 'disconnected'}">
            ${this.isConnected ? '●' : '○'}
          </span>
        </div>
        <div class="task-inbox-list">
          ${sortedRuns.length === 0 ? `
            <div class="task-inbox-empty">
              <p>No recent tasks</p>
              <p class="hint">Say "run my morning digest" to get started</p>
            </div>
          ` : sortedRuns.map(run => this.renderRun(run)).join('')}
        </div>
      </div>
    `;

    this.container.innerHTML = html;
  }

  /**
   * Render individual run item
   */
  private renderRun(run: JarvisRunSummary): string {
    const statusIcon = this.getStatusIcon(run.status);
    const statusClass = run.status.toLowerCase();
    const timeAgo = this.getTimeAgo(new Date(run.updated_at));

    return `
      <div class="task-item ${statusClass}" data-run-id="${run.id}">
        <div class="task-item-header">
          <span class="task-status">${statusIcon}</span>
          <span class="task-agent-name">${run.agent_name}</span>
          <span class="task-time">${timeAgo}</span>
        </div>
        ${run.summary ? `
          <div class="task-summary">
            ${this.truncateSummary(run.summary, 150)}
          </div>
        ` : ''}
      </div>
    `;
  }

  /**
   * Get status icon for run
   */
  private getStatusIcon(status: string): string {
    switch (status.toLowerCase()) {
      case 'success': return '✓';
      case 'failed': return '✗';
      case 'running': return '⟳';
      case 'queued': return '⋯';
      default: return '○';
    }
  }

  /**
   * Get human-readable time ago string
   */
  private getTimeAgo(date: Date): string {
    const seconds = Math.floor((Date.now() - date.getTime()) / 1000);

    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  /**
   * Truncate summary with ellipsis
   */
  private truncateSummary(text: string, maxLength: number): string {
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength).trim() + '...';
  }

  /**
   * Clean up and disconnect
   */
  destroy(): void {
    this.client.disconnectEventStream();
    this.isConnected = false;
    if (this.container) {
      this.container.innerHTML = '';
    }
  }

  /**
   * Manually refresh run history
   */
  async refresh(): Promise<void> {
    try {
      const runs = await this.client.listRuns({ limit: 20 });
      this.runs.clear();
      runs.forEach(run => this.runs.set(run.id, run));
      this.render();
    } catch (error) {
      console.error('Task Inbox: Refresh failed', error);
      this.options.onError?.(error as Error);
    }
  }
}

/**
 * Create and initialize a Task Inbox instance
 */
export async function createTaskInbox(
  container: HTMLElement,
  options: TaskInboxOptions
): Promise<TaskInbox> {
  const inbox = new TaskInbox(options);
  // Set container immediately so internal render calls work during initialization
  inbox.render(container);
  await inbox.initialize();
  return inbox;
}
