/**
 * Worker Progress Store
 *
 * External store that bridges the event bus to React components.
 * Provides state synchronization for worker progress UI.
 */

import { eventBus } from './event-bus';

export interface ToolCall {
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

export interface WorkerState {
  jobId: number;
  workerId?: string;
  task: string;
  status: 'spawned' | 'running' | 'complete' | 'failed';
  summary?: string;
  startedAt: number;
  completedAt?: number;
  toolCalls: Map<string, ToolCall>;
}

export interface WorkerProgressState {
  isActive: boolean;
  currentRunId: number | null;
  supervisorDone: boolean;
  reconnecting: boolean;
  workers: Map<number, WorkerState>;
}

type Listener = () => void;

/**
 * Worker progress store for React integration via useSyncExternalStore
 */
class WorkerProgressStore {
  private state: WorkerProgressState = {
    isActive: false,
    currentRunId: null,
    supervisorDone: false,
    reconnecting: false,
    workers: new Map(),
  };

  private listeners = new Set<Listener>();
  private workersByWorkerId: Map<string, WorkerState> = new Map();
  private clearTimeout: number | null = null;
  private tickerInterval: number | null = null;

  constructor() {
    this.subscribeToEvents();
  }

  /**
   * Get current state (for React)
   */
  getState(): WorkerProgressState {
    return this.state;
  }

  /**
   * Subscribe to state changes (for React useSyncExternalStore)
   */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Notify all listeners of state change
   */
  private notifyListeners(): void {
    this.listeners.forEach(listener => listener());
  }

  /**
   * Update state and notify listeners
   */
  private setState(updates: Partial<WorkerProgressState>): void {
    this.state = { ...this.state, ...updates };
    this.notifyListeners();
  }

  /**
   * Start the ticker for live duration updates on running tools
   */
  startTicker(): void {
    if (this.tickerInterval) return; // Already running

    this.tickerInterval = window.setInterval(() => {
      // Only re-render if there are running tools
      const hasRunningTools = Array.from(this.state.workers.values()).some(worker =>
        Array.from(worker.toolCalls.values()).some(tool => tool.status === 'running')
      );

      if (hasRunningTools) {
        this.notifyListeners(); // Trigger re-render for live duration
      }
    }, 500); // Update every 500ms for smooth duration display
  }

  /**
   * Stop the ticker
   */
  stopTicker(): void {
    if (this.tickerInterval) {
      clearInterval(this.tickerInterval);
      this.tickerInterval = null;
    }
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
   * Clear progress state
   */
  clear(): void {
    this.cancelPendingClear();
    this.stopTicker();
    this.workersByWorkerId.clear();
    this.setState({
      isActive: false,
      currentRunId: null,
      supervisorDone: false,
      reconnecting: false,
      workers: new Map(),
    });
  }

  /**
   * Set reconnecting state - shows UI immediately while SSE connects
   */
  setReconnecting(runId: number): void {
    console.log(`[WorkerProgress] Reconnecting to run ${runId}...`);
    this.setState({
      isActive: true,
      currentRunId: runId,
      reconnecting: true,
      supervisorDone: false,
      workers: new Map(),
    });
  }

  /**
   * Clear reconnecting state (called when SSE connects)
   */
  clearReconnecting(): void {
    if (this.state.reconnecting) {
      console.log('[WorkerProgress] Reconnection complete');
      this.setState({ reconnecting: false });
    }
  }

  /**
   * Subscribe to supervisor events
   */
  private subscribeToEvents(): void {
    eventBus.on('supervisor:started', (data) => {
      this.handleStarted(data.runId, data.task);
    });

    eventBus.on('supervisor:worker_spawned', (data) => {
      this.handleWorkerSpawned(data.jobId, data.task);
    });

    eventBus.on('supervisor:worker_started', (data) => {
      this.handleWorkerStarted(data.jobId, data.workerId);
    });

    eventBus.on('supervisor:worker_complete', (data) => {
      this.handleWorkerComplete(data.jobId, data.workerId, data.status, data.durationMs);
    });

    eventBus.on('supervisor:worker_summary', (data) => {
      this.handleWorkerSummary(data.jobId, data.summary);
    });

    eventBus.on('supervisor:complete', (data) => {
      this.handleComplete(data.runId, data.result, data.status);
    });

    eventBus.on('supervisor:deferred', () => {
      this.handleDeferred();
    });

    eventBus.on('supervisor:error', (data) => {
      this.handleError(data.message);
    });

    eventBus.on('supervisor:cleared', () => {
      this.clear();
    });

    // Tool event subscriptions
    eventBus.on('worker:tool_started', (data) => {
      this.handleToolStarted(data.workerId, data.toolCallId, data.toolName, data.argsPreview);
    });

    eventBus.on('worker:tool_completed', (data) => {
      this.handleToolCompleted(data.workerId, data.toolCallId, data.toolName, data.durationMs, data.resultPreview);
    });

    eventBus.on('worker:tool_failed', (data) => {
      this.handleToolFailed(data.workerId, data.toolCallId, data.toolName, data.durationMs, data.error);
    });
  }

  /**
   * Handle supervisor started
   */
  private handleStarted(runId: number, task: string): void {
    this.cancelPendingClear();
    this.workersByWorkerId.clear();
    this.setState({
      currentRunId: runId,
      supervisorDone: false,
      reconnecting: false, // Clear reconnecting - we're actively running
      workers: new Map(),
    });
    console.log(`[WorkerProgress] Tracking run ${runId}: ${task}`);
  }

  /**
   * Handle worker spawned
   */
  private handleWorkerSpawned(jobId: number, task: string): void {
    this.cancelPendingClear();

    // Activate UI on first worker
    if (!this.state.isActive) {
      this.startTicker();
      console.log(`[WorkerProgress] UI activated - workers detected`);
    }

    const newWorkers = new Map(this.state.workers);
    newWorkers.set(jobId, {
      jobId,
      task,
      status: 'spawned',
      startedAt: Date.now(),
      toolCalls: new Map(),
    });

    this.setState({
      isActive: true,
      reconnecting: false, // Clear reconnecting - we're receiving events
      workers: newWorkers,
    });

    console.log(`[WorkerProgress] Worker spawned: ${jobId} - ${task}`);
  }

  /**
   * Handle worker started
   */
  private handleWorkerStarted(jobId: number, workerId?: string): void {
    const newWorkers = new Map(this.state.workers);
    const worker = newWorkers.get(jobId);
    if (worker) {
      worker.status = 'running';
      worker.workerId = workerId;
      // Index by workerId for tool event lookups
      if (workerId) {
        this.workersByWorkerId.set(workerId, worker);
      }
      this.setState({ workers: newWorkers });
    }
    console.log(`[WorkerProgress] Worker started: ${jobId}`);
  }

  /**
   * Handle worker complete
   */
  private handleWorkerComplete(jobId: number, workerId?: string, status?: string, durationMs?: number): void {
    const newWorkers = new Map(this.state.workers);
    const worker = newWorkers.get(jobId);
    if (worker) {
      worker.status = status === 'success' ? 'complete' : 'failed';
      worker.workerId = workerId;
      worker.completedAt = Date.now();
      this.setState({ workers: newWorkers });
    }
    console.log(`[WorkerProgress] Worker complete: ${jobId} (${status}, ${durationMs}ms)`);
    this.maybeScheduleClear();
  }

  /**
   * Handle worker summary
   */
  private handleWorkerSummary(jobId: number, summary: string): void {
    const newWorkers = new Map(this.state.workers);
    const worker = newWorkers.get(jobId);
    if (worker) {
      worker.summary = summary;
      this.setState({ workers: newWorkers });
    }
    console.log(`[WorkerProgress] Worker summary: ${jobId} - ${summary}`);
    this.maybeScheduleClear();
  }

  /**
   * Find or create worker by workerId
   */
  private findOrCreateWorkerByWorkerId(workerId: string): WorkerState {
    // Fast path: lookup in index
    let worker = this.workersByWorkerId.get(workerId);
    if (worker) return worker;

    // Slow path: scan workers
    for (const w of this.state.workers.values()) {
      if (w.workerId === workerId) {
        this.workersByWorkerId.set(workerId, w);
        return w;
      }
    }

    // Create orphan worker
    console.warn(`[WorkerProgress] Creating orphan worker for workerId: ${workerId}`);
    const orphanJobId = -Date.now();
    const newWorkers = new Map(this.state.workers);
    worker = {
      jobId: orphanJobId,
      workerId,
      task: 'Worker (pending details)',
      status: 'running',
      startedAt: Date.now(),
      toolCalls: new Map(),
    };
    newWorkers.set(orphanJobId, worker);
    this.workersByWorkerId.set(workerId, worker);
    this.setState({ workers: newWorkers });
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

    // Clear reconnecting state - we're receiving real events now
    const stateUpdates: Partial<WorkerProgressState> = { reconnecting: false };
    if (!this.state.isActive) {
      stateUpdates.isActive = true;
    }
    if (Object.keys(stateUpdates).length > 0) {
      this.setState(stateUpdates);
    }

    const worker = this.findOrCreateWorkerByWorkerId(workerId);
    worker.toolCalls.set(toolCallId, {
      toolCallId,
      toolName,
      status: 'running',
      argsPreview,
      startedAt: Date.now(),
    });

    // Force update
    this.setState({ workers: new Map(this.state.workers) });
    console.log(`[WorkerProgress] Tool started: ${toolName} (${toolCallId})`);
  }

  /**
   * Handle tool completed
   */
  private handleToolCompleted(workerId: string, toolCallId: string, toolName: string, durationMs: number, resultPreview?: string): void {
    if (!workerId) {
      console.warn('[WorkerProgress] Dropping tool_completed with empty workerId');
      return;
    }

    if (!this.state.isActive) {
      this.setState({ isActive: true });
    }

    const worker = this.findOrCreateWorkerByWorkerId(workerId);
    let toolCall = worker.toolCalls.get(toolCallId);

    if (!toolCall) {
      console.warn(`[WorkerProgress] Tool completed without prior started: ${toolCallId}`);
      toolCall = {
        toolCallId,
        toolName: toolName || 'unknown',
        status: 'running',
        startedAt: Date.now() - durationMs,
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

    this.setState({ workers: new Map(this.state.workers) });
    console.log(`[WorkerProgress] Tool completed: ${toolCall.toolName} (${durationMs}ms)`);
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

    if (!this.state.isActive) {
      this.setState({ isActive: true });
    }

    const worker = this.findOrCreateWorkerByWorkerId(workerId);
    let toolCall = worker.toolCalls.get(toolCallId);

    if (!toolCall) {
      console.warn(`[WorkerProgress] Tool failed without prior started: ${toolCallId}`);
      toolCall = {
        toolCallId,
        toolName: toolName || 'unknown',
        status: 'running',
        startedAt: Date.now() - durationMs,
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

    this.setState({ workers: new Map(this.state.workers) });
    console.log(`[WorkerProgress] Tool failed: ${toolCall.toolName} - ${error}`);
    this.maybeScheduleClear();
  }

  /**
   * Check if there are pending workers
   */
  private hasPendingWorkers(): boolean {
    return Array.from(this.state.workers.values()).some((w) => w.status === 'spawned' || w.status === 'running');
  }

  /**
   * Maybe schedule clear if supervisor is done
   */
  private maybeScheduleClear(): void {
    if (!this.state.supervisorDone) return;
    if (this.hasPendingWorkers()) return;

    if (this.state.workers.size === 0) {
      this.scheduleClear(150);
      return;
    }

    this.scheduleClear(2000);
  }

  /**
   * Handle supervisor complete
   */
  private handleComplete(runId: number, result: string, status: string): void {
    console.log(`[WorkerProgress] Complete: ${runId} (${status})`);
    this.setState({ supervisorDone: true, reconnecting: false });
    this.maybeScheduleClear();
  }

  /**
   * Handle supervisor deferred
   */
  private handleDeferred(): void {
    console.log('[WorkerProgress] Deferred');
    this.setState({ supervisorDone: true, reconnecting: false });
    this.maybeScheduleClear();
  }

  /**
   * Handle error
   */
  private handleError(message: string): void {
    console.error(`[WorkerProgress] Error: ${message}`);
    this.scheduleClear(3000);
  }
}

// Export singleton instance
export const workerProgressStore = new WorkerProgressStore();
