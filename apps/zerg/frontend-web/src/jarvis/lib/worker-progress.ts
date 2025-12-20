/**
 * Worker Progress UI
 *
 * Shows live progress when the Supervisor delegates tasks to workers.
 * Only displays when workers are actively running - does NOT show a
 * "thinking" indicator (that's handled by the assistant message bubble).
 *
 * Shows:
 * - Worker spawn/start/complete status
 * - Live tool call activity within workers
 * - Worker summaries when complete
 */

import { eventBus } from './event-bus';

interface ToolCall {
  toolCallId: string;
  toolName: string;
  status: 'running' | 'completed' | 'failed';
  argsPreview?: string;
  resultPreview?: string;
  error?: string;
  startedAt: number;
  completedAt?: number;
  durationMs?: number;
}

interface WorkerState {
  jobId: number;
  workerId?: string;
  task: string;
  status: 'spawned' | 'running' | 'complete' | 'failed';
  summary?: string;
  startedAt: number;
  completedAt?: number;
  toolCalls: Map<string, ToolCall>;
}

/**
 * Display mode for worker progress UI
 * - 'floating': Fixed position toast at bottom-right (always visible)
 * - 'inline': Embedded in chat flow (original behavior, can scroll out of view)
 * - 'sticky': Sticky position at top of chat area (default, stays visible while scrolling)
 */
type DisplayMode = 'floating' | 'inline' | 'sticky';

export class WorkerProgressUI {
  private container: HTMLElement | null = null;
  private isActive = false;
  private currentRunId: number | null = null;
  private supervisorDone = false;
  private workers: Map<number, WorkerState> = new Map();
  private workersByWorkerId: Map<string, WorkerState> = new Map(); // Index for tool event lookups
  private unsubscribers: Array<() => void> = [];
  private tickerInterval: number | null = null; // For live duration updates
  private clearTimeout: number | null = null; // For delayed clear after complete/error
  private displayMode: DisplayMode = 'floating';

  constructor() {
    this.subscribeToEvents();
  }

  /**
   * Initialize with container element
   *
   * @param containerId - DOM id for the container element
   * @param mode - Display mode: 'sticky' (default), 'floating', or 'inline'
   */
  initialize(containerId: string = 'worker-progress', mode: DisplayMode = 'sticky'): void {
    this.displayMode = mode;
    this.container = document.getElementById(containerId);

    if (!this.container) {
      this.container = document.createElement('div');
      this.container.id = containerId;
      document.body.appendChild(this.container);
    }

    // Normalize container classes so CSS + JS display toggles work.
    // React app may pre-render a placeholder div (sometimes with `hidden`).
    this.container.classList.remove('hidden');
    this.container.classList.add('worker-progress');
    this.container.classList.remove('worker-progress--floating', 'worker-progress--sticky');

    if (mode === 'floating') {
      // Floating mode: ensure it's attached to <body> so it stays visible even if chat scrolls.
      this.container.classList.add('worker-progress--floating');
      if (this.container.parentElement !== document.body) {
        document.body.appendChild(this.container);
      }
    } else if (mode === 'sticky') {
      // Sticky mode: place inside .transcript as first child, sticks to top while scrolling.
      this.container.classList.add('worker-progress--sticky');
      const transcript = document.querySelector('.transcript');
      if (transcript && this.container.parentElement !== transcript) {
        transcript.insertBefore(this.container, transcript.firstChild);
      }
    } else {
      // Inline mode: place inside chat container above transcript.
      const chatContainer = document.querySelector('.chat-container');
      const transcript = document.getElementById('transcript');
      if (chatContainer && transcript && transcript.parentElement === chatContainer) {
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

    // Tool event subscriptions (Phase 2: Activity Ticker)
    this.unsubscribers.push(
      eventBus.on('worker:tool_started', (data) => {
        this.handleToolStarted(data.workerId, data.toolCallId, data.toolName, data.argsPreview);
      })
    );

    this.unsubscribers.push(
      eventBus.on('worker:tool_completed', (data) => {
        this.handleToolCompleted(data.workerId, data.toolCallId, data.toolName, data.durationMs, data.resultPreview);
      })
    );

    this.unsubscribers.push(
      eventBus.on('worker:tool_failed', (data) => {
        this.handleToolFailed(data.workerId, data.toolCallId, data.toolName, data.durationMs, data.error);
      })
    );
  }

  /**
   * Handle supervisor started
   * Just tracks the run ID - UI only shows when workers are spawned
   */
  private handleStarted(runId: number, task: string): void {
    // Cancel any pending clear from previous supervisor complete/error
    this.cancelPendingClear();

    // Track run but don't show UI yet - wait for workers
    this.currentRunId = runId;
    this.supervisorDone = false;
    this.workers.clear();
    this.workersByWorkerId.clear();
    console.log(`[WorkerProgress] Tracking run ${runId}: ${task}`);
    // Don't render or activate - UI only shows when workers spawn
  }

  /**
   * Cancel any pending delayed clear
   */
  private cancelPendingClear(): void {
    if (this.clearTimeout !== null) {
      clearTimeout(this.clearTimeout);
      this.clearTimeout = null;
    }
  }

  /**
   * Briefly pulse the container to draw user attention
   */
  private pulseAttention(): void {
    if (!this.container) return;

    this.container.classList.add('worker-progress--attention');
    setTimeout(() => {
      this.container?.classList.remove('worker-progress--attention');
    }, 1500);
  }

  /**
   * Handle supervisor thinking
   * No-op - the assistant message bubble shows typing indicator
   */
  private handleThinking(message: string): void {
    console.log(`[WorkerProgress] Supervisor thinking: ${message}`);
    // Don't show UI - assistant bubble handles the typing indicator
  }

  /**
   * Handle worker spawned
   * This is when the UI first becomes visible
   */
  private handleWorkerSpawned(jobId: number, task: string): void {
    // A worker spawning means this run is still active; cancel any pending clear
    this.cancelPendingClear();

    // Activate UI on first worker
    if (!this.isActive) {
      this.isActive = true;
      this.startTicker(); // Start live duration updates
      this.pulseAttention(); // Draw attention
      console.log(`[WorkerProgress] UI activated - workers detected`);
    }

    this.workers.set(jobId, {
      jobId,
      task,
      status: 'spawned',
      startedAt: Date.now(),
      toolCalls: new Map(),
    });
    console.log(`[WorkerProgress] Worker spawned: ${jobId} - ${task}`);
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
      // Index by workerId for tool event lookups
      if (workerId) {
        this.workersByWorkerId.set(workerId, worker);
      }
    }
    console.log(`[WorkerProgress] Worker started: ${jobId}`);
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
    console.log(`[WorkerProgress] Worker complete: ${jobId} (${status}, ${durationMs}ms)`);
    this.render();
    this.maybeScheduleClear();
  }

  /**
   * Handle worker summary
   */
  private handleWorkerSummary(jobId: number, summary: string): void {
    const worker = this.workers.get(jobId);
    if (worker) {
      worker.summary = summary;
    }
    console.log(`[WorkerProgress] Worker summary: ${jobId} - ${summary}`);
    this.render();
    this.maybeScheduleClear();
  }

  /**
   * Find or create worker by workerId (filesystem artifact ID).
   * Creates an orphan worker if tool events arrive before worker_started.
   */
  private findOrCreateWorkerByWorkerId(workerId: string): WorkerState {
    // Fast path: lookup in index
    let worker = this.workersByWorkerId.get(workerId);
    if (worker) return worker;

    // Slow path: scan workers (handles race where index wasn't updated yet)
    for (const w of this.workers.values()) {
      if (w.workerId === workerId) {
        this.workersByWorkerId.set(workerId, w);
        return w;
      }
    }

    // No worker found - create an orphan worker for this workerId
    // This handles the case where tool events arrive before worker_started
    console.warn(`[WorkerProgress] Creating orphan worker for workerId: ${workerId}`);
    const orphanJobId = -Date.now(); // Negative ID to avoid collisions
    worker = {
      jobId: orphanJobId,
      workerId,
      task: 'Worker (pending details)',
      status: 'running',
      startedAt: Date.now(),
      toolCalls: new Map(),
    };
    this.workers.set(orphanJobId, worker);
    this.workersByWorkerId.set(workerId, worker);
    return worker;
  }

  /**
   * Handle tool started
   */
  private handleToolStarted(workerId: string, toolCallId: string, toolName: string, argsPreview?: string): void {
    if (!workerId) {
      console.warn('[WorkerProgress] Dropping tool_started with empty workerId');
      return;
    }

    // Tool activity implies the run is still active.
    if (!this.isActive) {
      this.isActive = true;
    }

    const worker = this.findOrCreateWorkerByWorkerId(workerId);
    worker.toolCalls.set(toolCallId, {
      toolCallId,
      toolName,
      status: 'running',
      argsPreview,
      startedAt: Date.now(),
    });
    console.log(`[WorkerProgress] Tool started: ${toolName} (${toolCallId})`);
    this.render();
  }

  /**
   * Handle tool completed
   */
  private handleToolCompleted(workerId: string, toolCallId: string, toolName: string, durationMs: number, resultPreview?: string): void {
    if (!workerId) {
      console.warn('[WorkerProgress] Dropping tool_completed with empty workerId');
      return;
    }

    // Tool activity implies the run is still active.
    if (!this.isActive) {
      this.isActive = true;
    }

    const worker = this.findOrCreateWorkerByWorkerId(workerId);
    let toolCall = worker.toolCalls.get(toolCallId);

    // Create entry if tool_started was missed
    if (!toolCall) {
      console.warn(`[WorkerProgress] Tool completed without prior started: ${toolCallId}`);
      toolCall = {
        toolCallId,
        toolName: toolName || 'unknown',
        status: 'running',
        startedAt: Date.now() - durationMs, // Backdate based on duration
      };
      worker.toolCalls.set(toolCallId, toolCall);
    }

    if (toolName && toolCall.toolName === 'unknown') {
      toolCall.toolName = toolName;
    }

    toolCall.status = 'completed';
    toolCall.durationMs = durationMs;
    toolCall.resultPreview = resultPreview;
    toolCall.completedAt = Date.now();
    console.log(`[WorkerProgress] Tool completed: ${toolCall.toolName} (${durationMs}ms)`);
    this.render();
    this.maybeScheduleClear();
  }

  /**
   * Handle tool failed
   */
  private handleToolFailed(workerId: string, toolCallId: string, toolName: string, durationMs: number, error: string): void {
    if (!workerId) {
      console.warn('[WorkerProgress] Dropping tool_failed with empty workerId');
      return;
    }

    // Tool activity implies the run is still active.
    if (!this.isActive) {
      this.isActive = true;
    }

    const worker = this.findOrCreateWorkerByWorkerId(workerId);
    let toolCall = worker.toolCalls.get(toolCallId);

    // Create entry if tool_started was missed
    if (!toolCall) {
      console.warn(`[WorkerProgress] Tool failed without prior started: ${toolCallId}`);
      toolCall = {
        toolCallId,
        toolName: toolName || 'unknown',
        status: 'running',
        startedAt: Date.now() - durationMs, // Backdate based on duration
      };
      worker.toolCalls.set(toolCallId, toolCall);
    }

    if (toolName && toolCall.toolName === 'unknown') {
      toolCall.toolName = toolName;
    }

    toolCall.status = 'failed';
    toolCall.durationMs = durationMs;
    toolCall.error = error;
    toolCall.completedAt = Date.now();
    console.log(`[WorkerProgress] Tool failed: ${toolCall.toolName} - ${error}`);
    this.render();
    this.maybeScheduleClear();
  }

  private hasPendingWorkers(): boolean {
    return Array.from(this.workers.values()).some((w) => w.status === 'spawned' || w.status === 'running');
  }

  private maybeScheduleClear(): void {
    if (!this.supervisorDone) return;
    if (this.hasPendingWorkers()) return;

    // No pending workers. If we never saw any workers, clear quickly (thinking-only run).
    if (this.workers.size === 0) {
      this.scheduleClear(150);
      return;
    }

    // Workers finished; keep the delegating UI briefly so user can read status.
    this.scheduleClear(2000);
  }

  /**
   * Handle supervisor complete
   * Clears quickly if no workers, or waits briefly if workers were shown
   */
  private handleComplete(runId: number, result: string, status: string): void {
    console.log(`[WorkerProgress] Complete: ${runId} (${status})`);
    this.supervisorDone = true;

    // If no workers are involved, clear fast; otherwise wait for workers to finish.
    this.maybeScheduleClear();
  }

  /**
   * Handle error
   */
  private handleError(message: string): void {
    console.error(`[WorkerProgress] Error: ${message}`);
    // Show error briefly then clear
    this.scheduleClear(3000);
  }

  /**
   * Schedule a delayed clear (cancellable if new supervisor starts)
   */
  private scheduleClear(delayMs: number): void {
    this.cancelPendingClear();
    this.clearTimeout = window.setTimeout(() => {
      this.clearTimeout = null;
      this.clear();
    }, delayMs);
  }

  /**
   * Clear progress UI
   */
  clear(): void {
    this.cancelPendingClear();
    this.isActive = false;
    this.currentRunId = null;
    this.supervisorDone = false;
    this.workers.clear();
    this.workersByWorkerId.clear();
    this.stopTicker();
    this.render();
  }

  /**
   * Start the ticker for live duration updates on running tools
   */
  private startTicker(): void {
    if (this.tickerInterval) return; // Already running

    this.tickerInterval = window.setInterval(() => {
      // Only re-render if there are running tools
      const hasRunningTools = Array.from(this.workers.values()).some(worker =>
        Array.from(worker.toolCalls.values()).some(tool => tool.status === 'running')
      );

      if (hasRunningTools) {
        this.render();
      }
    }, 500); // Update every 500ms for smooth duration display
  }

  /**
   * Stop the ticker
   */
  private stopTicker(): void {
    if (this.tickerInterval) {
      clearInterval(this.tickerInterval);
      this.tickerInterval = null;
    }
  }

  /**
   * Render the progress UI
   * Only shows when workers are active
   */
  private render(): void {
    if (!this.container) return;

    if (!this.isActive) {
      this.container.innerHTML = '';
      this.container.style.display = 'none';
      this.container.classList.remove('worker-progress--active');
      return;
    }

    this.container.style.display = 'block';
    this.container.classList.add('worker-progress--active');

    // Full delegating modal with workers
    const workersArray = Array.from(this.workers.values());
    const runningWorkers = workersArray.filter(w => w.status === 'running' || w.status === 'spawned');

    this.container.innerHTML = `
      <div class="worker-progress-content">
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

    const toolCallsArray = Array.from(worker.toolCalls.values());
    const hasToolCalls = toolCallsArray.length > 0;

    return `
      <div class="supervisor-worker ${statusClass}">
        <div class="worker-header">
          <span class="worker-icon">${statusIcon}</span>
          <span class="worker-task">${this.escapeHtml(taskPreview)}</span>
        </div>
        ${hasToolCalls ? `
          <div class="worker-tools">
            ${toolCallsArray.map(tool => this.renderToolCall(tool)).join('')}
          </div>
        ` : ''}
        ${worker.summary ? `<div class="worker-summary">${this.escapeHtml(worker.summary)}</div>` : ''}
      </div>
    `;
  }

  /**
   * Render a single tool call
   */
  private renderToolCall(tool: ToolCall): string {
    const statusIcon = this.getToolStatusIcon(tool.status);
    const statusClass = `tool-status-${tool.status}`;
    const duration = tool.durationMs ? `${tool.durationMs}ms` : this.getElapsedTime(tool.startedAt);

    // Format tool name for display (e.g., ssh_exec -> ssh_exec)
    const toolNameDisplay = tool.toolName;

    // Show args preview if running, result/error preview if done
    let preview = '';
    if (tool.status === 'running' && tool.argsPreview) {
      preview = this.truncatePreview(tool.argsPreview, 50);
    } else if (tool.status === 'failed' && tool.error) {
      preview = this.truncatePreview(tool.error, 50);
    }

    return `
      <div class="worker-tool ${statusClass}">
        <span class="tool-icon">${statusIcon}</span>
        <span class="tool-name">${this.escapeHtml(toolNameDisplay)}</span>
        ${preview ? `<span class="tool-preview">${this.escapeHtml(preview)}</span>` : ''}
        <span class="tool-duration">${duration}</span>
      </div>
    `;
  }

  /**
   * Get icon for tool status
   */
  private getToolStatusIcon(status: string): string {
    switch (status) {
      case 'running':
        return '⏳';
      case 'completed':
        return '✓';
      case 'failed':
        return '✗';
      default:
        return '•';
    }
  }

  /**
   * Get elapsed time since start
   */
  private getElapsedTime(startedAt: number): string {
    const elapsed = Date.now() - startedAt;
    if (elapsed < 1000) {
      return `${elapsed}ms`;
    }
    return `${(elapsed / 1000).toFixed(1)}s`;
  }

  /**
   * Truncate preview text
   */
  private truncatePreview(text: string, maxLen: number): string {
    if (text.length <= maxLen) return text;
    return text.substring(0, maxLen - 3) + '...';
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
    this.stopTicker();
    this.clear();
  }
}

// Export singleton instance
// Named 'supervisorProgress' for backward compatibility with existing imports
export const supervisorProgress = new WorkerProgressUI();

// Also export with cleaner name for new code
export { supervisorProgress as workerProgress };
