/**
 * Supervisor Progress UI
 *
 * Renders visual progress indicators when Jarvis routes tasks to the
 * Zerg Supervisor for investigation. Shows:
 * - "Investigating..." state with animated spinner
 * - Worker spawn/complete status
 * - Final result
 */

import { eventBus } from './event-bus';

interface WorkerState {
  jobId: number;
  workerId?: string;
  task: string;
  status: 'spawned' | 'running' | 'complete' | 'failed';
  summary?: string;
  startedAt: number;
  completedAt?: number;
}

export class SupervisorProgressUI {
  private container: HTMLElement | null = null;
  private isActive = false;
  private currentRunId: number | null = null;
  private workers: Map<number, WorkerState> = new Map();
  private unsubscribers: Array<() => void> = [];

  constructor() {
    this.subscribeToEvents();
  }

  /**
   * Initialize with container element
   */
  initialize(containerId: string = 'supervisor-progress'): void {
    this.container = document.getElementById(containerId);
    if (!this.container) {
      // Create container if it doesn't exist
      this.container = document.createElement('div');
      this.container.id = containerId;
      this.container.className = 'supervisor-progress';

      // Insert before the transcript in the chat container
      const chatContainer = document.querySelector('.chat-container');
      const transcript = document.getElementById('transcript');
      if (chatContainer && transcript) {
        chatContainer.insertBefore(this.container, transcript);
      }
    }
    this.render();
  }

  /**
   * Subscribe to supervisor events
   */
  private subscribeToEvents(): void {
    this.unsubscribers.push(
      eventBus.on('supervisor:started', (data) => {
        this.handleStarted(data.runId, data.task);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:thinking', (data) => {
        this.handleThinking(data.message);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:worker_spawned', (data) => {
        this.handleWorkerSpawned(data.jobId, data.task);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:worker_started', (data) => {
        this.handleWorkerStarted(data.jobId, data.workerId);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:worker_complete', (data) => {
        this.handleWorkerComplete(data.jobId, data.workerId, data.status, data.durationMs);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:worker_summary', (data) => {
        this.handleWorkerSummary(data.jobId, data.summary);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:complete', (data) => {
        this.handleComplete(data.runId, data.result, data.status);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:error', (data) => {
        this.handleError(data.message);
      })
    );

    this.unsubscribers.push(
      eventBus.on('supervisor:cleared', () => {
        this.clear();
      })
    );
  }

  /**
   * Handle supervisor started
   */
  private handleStarted(runId: number, task: string): void {
    this.isActive = true;
    this.currentRunId = runId;
    this.workers.clear();
    console.log(`[SupervisorProgress] Started run ${runId}: ${task}`);
    this.render();
  }

  /**
   * Handle supervisor thinking
   */
  private handleThinking(message: string): void {
    // Update UI to show thinking state
    console.log(`[SupervisorProgress] Thinking: ${message}`);
    this.render();
  }

  /**
   * Handle worker spawned
   */
  private handleWorkerSpawned(jobId: number, task: string): void {
    this.workers.set(jobId, {
      jobId,
      task,
      status: 'spawned',
      startedAt: Date.now(),
    });
    console.log(`[SupervisorProgress] Worker spawned: ${jobId} - ${task}`);
    this.render();
  }

  /**
   * Handle worker started
   */
  private handleWorkerStarted(jobId: number, workerId?: string): void {
    const worker = this.workers.get(jobId);
    if (worker) {
      worker.status = 'running';
      worker.workerId = workerId;
    }
    console.log(`[SupervisorProgress] Worker started: ${jobId}`);
    this.render();
  }

  /**
   * Handle worker complete
   */
  private handleWorkerComplete(jobId: number, workerId?: string, status?: string, durationMs?: number): void {
    const worker = this.workers.get(jobId);
    if (worker) {
      worker.status = status === 'success' ? 'complete' : 'failed';
      worker.workerId = workerId;
      worker.completedAt = Date.now();
    }
    console.log(`[SupervisorProgress] Worker complete: ${jobId} (${status}, ${durationMs}ms)`);
    this.render();
  }

  /**
   * Handle worker summary
   */
  private handleWorkerSummary(jobId: number, summary: string): void {
    const worker = this.workers.get(jobId);
    if (worker) {
      worker.summary = summary;
    }
    console.log(`[SupervisorProgress] Worker summary: ${jobId} - ${summary}`);
    this.render();
  }

  /**
   * Handle supervisor complete
   */
  private handleComplete(runId: number, result: string, status: string): void {
    console.log(`[SupervisorProgress] Complete: ${runId} (${status})`);
    // Keep showing for a moment then clear
    setTimeout(() => {
      this.clear();
    }, 2000);
  }

  /**
   * Handle error
   */
  private handleError(message: string): void {
    console.error(`[SupervisorProgress] Error: ${message}`);
    // Show error briefly then clear
    setTimeout(() => {
      this.clear();
    }, 3000);
  }

  /**
   * Clear progress UI
   */
  clear(): void {
    this.isActive = false;
    this.currentRunId = null;
    this.workers.clear();
    this.render();
  }

  /**
   * Render the progress UI
   */
  private render(): void {
    if (!this.container) return;

    if (!this.isActive) {
      this.container.innerHTML = '';
      this.container.style.display = 'none';
      return;
    }

    this.container.style.display = 'block';

    const workersArray = Array.from(this.workers.values());
    const runningWorkers = workersArray.filter(w => w.status === 'running' || w.status === 'spawned');
    const completedWorkers = workersArray.filter(w => w.status === 'complete' || w.status === 'failed');

    this.container.innerHTML = `
      <div class="supervisor-progress-content">
        <div class="supervisor-status">
          <div class="supervisor-spinner"></div>
          <span class="supervisor-label">Investigating...</span>
        </div>
        ${workersArray.length > 0 ? `
          <div class="supervisor-workers">
            ${workersArray.map(worker => this.renderWorker(worker)).join('')}
          </div>
        ` : ''}
        ${runningWorkers.length > 0 ? `
          <div class="supervisor-active-count">
            ${runningWorkers.length} worker${runningWorkers.length > 1 ? 's' : ''} running...
          </div>
        ` : ''}
      </div>
    `;
  }

  /**
   * Render a single worker status
   */
  private renderWorker(worker: WorkerState): string {
    const statusIcon = this.getWorkerStatusIcon(worker.status);
    const statusClass = `worker-status-${worker.status}`;
    const taskPreview = worker.task.length > 40
      ? worker.task.substring(0, 40) + '...'
      : worker.task;

    return `
      <div class="supervisor-worker ${statusClass}">
        <span class="worker-icon">${statusIcon}</span>
        <span class="worker-task">${this.escapeHtml(taskPreview)}</span>
        ${worker.summary ? `<span class="worker-summary">${this.escapeHtml(worker.summary)}</span>` : ''}
      </div>
    `;
  }

  /**
   * Get icon for worker status
   */
  private getWorkerStatusIcon(status: string): string {
    switch (status) {
      case 'spawned':
        return '🚀';
      case 'running':
        return '⚙️';
      case 'complete':
        return '✅';
      case 'failed':
        return '❌';
      default:
        return '📋';
    }
  }

  /**
   * Escape HTML to prevent XSS
   */
  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Cleanup
   */
  destroy(): void {
    this.unsubscribers.forEach(unsub => unsub());
    this.unsubscribers = [];
    this.clear();
  }
}

// Export singleton instance
export const supervisorProgress = new SupervisorProgressUI();
