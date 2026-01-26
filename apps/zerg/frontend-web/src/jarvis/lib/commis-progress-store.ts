/**
 * Commis Progress Store
 *
 * External store that bridges the event bus to React components.
 * Provides state synchronization for commis progress UI.
 */

import { eventBus } from './event-bus';
import { logger } from '../core';

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

export interface CommisState {
  jobId: number;
  commisId?: string;
  task: string;
  status: 'spawned' | 'running' | 'complete' | 'failed';
  summary?: string;
  startedAt: number;
  completedAt?: number;
  toolCalls: Map<string, ToolCall>;
}

export interface CommisProgressState {
  isActive: boolean;
  currentCourseId: number | null;
  conciergeDone: boolean;
  reconnecting: boolean;
  commis: Map<number, CommisState>;
}

type Listener = () => void;

/**
 * Commis progress store for React integration via useSyncExternalStore
 */
class CommisProgressStore {
  private state: CommisProgressState = {
    isActive: false,
    currentCourseId: null,
    conciergeDone: false,
    reconnecting: false,
    commis: new Map(),
  };

  private listeners = new Set<Listener>();
  private commisByCommisId: Map<string, CommisState> = new Map();
  private clearTimeout: number | null = null;
  private tickerInterval: number | null = null;

  constructor() {
    this.subscribeToEvents();
  }

  private _rekeyCommis(newCommis: Map<number, CommisState>, commis: CommisState, newJobId: number): void {
    // Remove old key if present (e.g. orphan negative id), then re-insert under real job id.
    if (newCommis.has(commis.jobId)) {
      newCommis.delete(commis.jobId);
    }
    commis.jobId = newJobId;
    newCommis.set(newJobId, commis);
  }

  private _resolveCommisForJobEvent(newCommis: Map<number, CommisState>, jobId: number, commisId?: string): CommisState | undefined {
    const direct = newCommis.get(jobId);
    if (direct) return direct;

    if (!commisId) return undefined;

    const byCommisId = this.commisByCommisId.get(commisId);
    if (!byCommisId) return undefined;

    // If we previously created an orphan commis from tool events, re-key it now that we have a real job id.
    if (byCommisId.jobId !== jobId) {
      this._rekeyCommis(newCommis, byCommisId, jobId);
    }

    return byCommisId;
  }

  /**
   * Get current state (for React)
   */
  getState(): CommisProgressState {
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
  private setState(updates: Partial<CommisProgressState>): void {
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
      const hasRunningTools = Array.from(this.state.commis.values()).some(commis =>
        Array.from(commis.toolCalls.values()).some(tool => tool.status === 'running')
      );

      if (hasRunningTools) {
        // Force new state reference so useSyncExternalStore detects the "change"
        // Without this, getState() returns the same object and React won't re-render
        this.state = { ...this.state };
        this.notifyListeners();
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
   * Schedule a delayed clear (cancellable if new concierge starts)
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
    this.commisByCommisId.clear();
    this.setState({
      isActive: false,
      currentCourseId: null,
      conciergeDone: false,
      reconnecting: false,
      commis: new Map(),
    });
  }

  /**
   * Set reconnecting state - shows UI immediately while SSE connects
   */
  setReconnecting(courseId: number): void {
    logger.debug(`[CommisProgress] Reconnecting to course ${courseId}...`);
    this.setState({
      isActive: true,
      currentCourseId: courseId,
      reconnecting: true,
      conciergeDone: false,
      commis: new Map(),
    });
  }

  /**
   * Clear reconnecting state (called when SSE connects)
   */
  clearReconnecting(): void {
    if (this.state.reconnecting) {
      logger.debug('[CommisProgress] Reconnection complete');
      this.setState({ reconnecting: false });
    }
  }

  /**
   * Subscribe to concierge events
   */
  private subscribeToEvents(): void {
    eventBus.on('concierge:started', (data) => {
      this.handleStarted(data.courseId, data.task);
    });

    eventBus.on('concierge:commis_spawned', (data) => {
      this.handleCommisSpawned(data.jobId, data.task);
    });

    eventBus.on('concierge:commis_started', (data) => {
      this.handleCommisStarted(data.jobId, data.commisId);
    });

    eventBus.on('concierge:commis_complete', (data) => {
      this.handleCommisComplete(data.jobId, data.commisId, data.status, data.durationMs);
    });

    eventBus.on('concierge:commis_summary', (data) => {
      this.handleCommisSummary(data.jobId, data.summary);
    });

    eventBus.on('concierge:complete', (data) => {
      this.handleComplete(data.courseId, data.result, data.status);
    });

    eventBus.on('concierge:deferred', () => {
      this.handleDeferred();
    });

    eventBus.on('concierge:error', (data) => {
      this.handleError(data.message);
    });

    eventBus.on('concierge:cleared', () => {
      this.clear();
    });

    // Tool event subscriptions
    eventBus.on('commis:tool_started', (data) => {
      this.handleToolStarted(data.commisId, data.toolCallId, data.toolName, data.argsPreview);
    });

    eventBus.on('commis:tool_completed', (data) => {
      this.handleToolCompleted(data.commisId, data.toolCallId, data.toolName, data.durationMs, data.resultPreview);
    });

    eventBus.on('commis:tool_failed', (data) => {
      this.handleToolFailed(data.commisId, data.toolCallId, data.toolName, data.durationMs, data.error);
    });
  }

  /**
   * Handle concierge started
   */
  private handleStarted(courseId: number, task: string): void {
    this.cancelPendingClear();
    this.commisByCommisId.clear();
    this.setState({
      currentCourseId: courseId,
      conciergeDone: false,
      reconnecting: false, // Clear reconnecting - we're actively running
      commis: new Map(),
    });
    logger.debug(`[CommisProgress] Tracking course ${courseId}: ${task}`);
  }

  /**
   * Handle commis spawned
   */
  private handleCommisSpawned(jobId: number, task: string): void {
    this.cancelPendingClear();

    // Activate UI on first commis
    if (!this.state.isActive) {
      this.startTicker();
      logger.debug(`[CommisProgress] UI activated - commis detected`);
    }

    const newCommis = new Map(this.state.commis);
    newCommis.set(jobId, {
      jobId,
      task,
      status: 'spawned',
      startedAt: Date.now(),
      toolCalls: new Map(),
    });

    this.setState({
      isActive: true,
      reconnecting: false, // Clear reconnecting - we're receiving events
      commis: newCommis,
    });

    logger.debug(`[CommisProgress] Commis spawned: ${jobId} - ${task}`);
  }

  /**
   * Handle commis started
   */
  private handleCommisStarted(jobId: number, commisId?: string): void {
    const newCommis = new Map(this.state.commis);
    const commis = this._resolveCommisForJobEvent(newCommis, jobId, commisId);
    if (commis) {
      commis.status = 'running';
      commis.commisId = commisId;
      // Index by commisId for tool event lookups
      if (commisId) {
        this.commisByCommisId.set(commisId, commis);
      }
      this.setState({ commis: newCommis });
    }
    logger.debug(`[CommisProgress] Commis started: ${jobId}`);
  }

  /**
   * Handle commis complete
   */
  private handleCommisComplete(jobId: number, commisId?: string, status?: string, durationMs?: number): void {
    const newCommis = new Map(this.state.commis);
    const commis = this._resolveCommisForJobEvent(newCommis, jobId, commisId);
    if (commis) {
      if (commis.status === 'complete' || commis.status === 'failed') {
        logger.debug(`[CommisProgress] Duplicate commis_complete ignored: ${jobId} (${status}, ${durationMs}ms)`);
        return;
      }
      commis.status = status === 'success' ? 'complete' : 'failed';
      commis.commisId = commisId;
      commis.completedAt = Date.now();
      this.setState({ commis: newCommis });
    }
    logger.debug(`[CommisProgress] Commis complete: ${jobId} (${status}, ${durationMs}ms)`);
    this.maybeScheduleClear();
  }

  /**
   * Handle commis summary
   */
  private handleCommisSummary(jobId: number, summary: string): void {
    const newCommis = new Map(this.state.commis);
    const commis = newCommis.get(jobId);
    if (commis) {
      commis.summary = summary;
      this.setState({ commis: newCommis });
    }
    logger.debug(`[CommisProgress] Commis summary: ${jobId} - ${summary}`);
    this.maybeScheduleClear();
  }

  /**
   * Find or create commis by commisId
   */
  private findOrCreateCommisByCommisId(commisId: string): CommisState {
    // Fast path: lookup in index
    let commis = this.commisByCommisId.get(commisId);
    if (commis) return commis;

    // Slow path: scan commis
    for (const w of this.state.commis.values()) {
      if (w.commisId === commisId) {
        this.commisByCommisId.set(commisId, w);
        return w;
      }
    }

    // Create orphan commis
    logger.warn(`[CommisProgress] Creating orphan commis for commisId: ${commisId}`);
    const orphanJobId = -Date.now();
    const newCommis = new Map(this.state.commis);
    commis = {
      jobId: orphanJobId,
      commisId,
      task: 'Commis (pending details)',
      status: 'running',
      startedAt: Date.now(),
      toolCalls: new Map(),
    };
    newCommis.set(orphanJobId, commis);
    this.commisByCommisId.set(commisId, commis);
    this.setState({ commis: newCommis });
    return commis;
  }

  /**
   * Handle tool started
   */
  private handleToolStarted(commisId: string, toolCallId: string, toolName: string, argsPreview?: string): void {
    if (!commisId) {
      logger.warn('[CommisProgress] Dropping tool_started with empty commisId');
      return;
    }

    // Clear reconnecting state - we're receiving real events now
    const stateUpdates: Partial<CommisProgressState> = { reconnecting: false };
    if (!this.state.isActive) {
      stateUpdates.isActive = true;
    }
    if (Object.keys(stateUpdates).length > 0) {
      this.setState(stateUpdates);
    }

    const commis = this.findOrCreateCommisByCommisId(commisId);
    commis.toolCalls.set(toolCallId, {
      toolCallId,
      toolName,
      status: 'running',
      argsPreview,
      startedAt: Date.now(),
    });

    // Force update
    this.setState({ commis: new Map(this.state.commis) });
    logger.debug(`[CommisProgress] Tool started: ${toolName} (${toolCallId})`);
  }

  /**
   * Handle tool completed
   */
  private handleToolCompleted(commisId: string, toolCallId: string, toolName: string, durationMs: number, resultPreview?: string): void {
    if (!commisId) {
      logger.warn('[CommisProgress] Dropping tool_completed with empty commisId');
      return;
    }

    if (!this.state.isActive) {
      this.setState({ isActive: true });
    }

    const commis = this.findOrCreateCommisByCommisId(commisId);
    let toolCall = commis.toolCalls.get(toolCallId);

    if (!toolCall) {
      logger.warn(`[CommisProgress] Tool completed without prior started: ${toolCallId}`);
      toolCall = {
        toolCallId,
        toolName: toolName || 'unknown',
        status: 'running',
        startedAt: Date.now() - durationMs,
      };
      commis.toolCalls.set(toolCallId, toolCall);
    }

    if (toolName && toolCall.toolName === 'unknown') {
      toolCall.toolName = toolName;
    }

    toolCall.status = 'completed';
    toolCall.durationMs = durationMs;
    toolCall.resultPreview = resultPreview;
    toolCall.completedAt = Date.now();

    this.setState({ commis: new Map(this.state.commis) });
    logger.debug(`[CommisProgress] Tool completed: ${toolCall.toolName} (${durationMs}ms)`);
    this.maybeScheduleClear();
  }

  /**
   * Handle tool failed
   */
  private handleToolFailed(commisId: string, toolCallId: string, toolName: string, durationMs: number, error: string): void {
    if (!commisId) {
      logger.warn('[CommisProgress] Dropping tool_failed with empty commisId');
      return;
    }

    if (!this.state.isActive) {
      this.setState({ isActive: true });
    }

    const commis = this.findOrCreateCommisByCommisId(commisId);
    let toolCall = commis.toolCalls.get(toolCallId);

    if (!toolCall) {
      logger.warn(`[CommisProgress] Tool failed without prior started: ${toolCallId}`);
      toolCall = {
        toolCallId,
        toolName: toolName || 'unknown',
        status: 'running',
        startedAt: Date.now() - durationMs,
      };
      commis.toolCalls.set(toolCallId, toolCall);
    }

    if (toolName && toolCall.toolName === 'unknown') {
      toolCall.toolName = toolName;
    }

    toolCall.status = 'failed';
    toolCall.durationMs = durationMs;
    toolCall.error = error;
    toolCall.completedAt = Date.now();

    this.setState({ commis: new Map(this.state.commis) });
    logger.debug(`[CommisProgress] Tool failed: ${toolCall.toolName} - ${error}`);
    this.maybeScheduleClear();
  }

  /**
   * Check if there are pending commis
   */
  private hasPendingCommis(): boolean {
    return Array.from(this.state.commis.values()).some((w) => w.status === 'spawned' || w.status === 'running');
  }

  /**
   * Maybe schedule clear if concierge is done
   */
  private maybeScheduleClear(): void {
    if (!this.state.conciergeDone) return;
    if (this.hasPendingCommis()) return;

    if (this.state.commis.size === 0) {
      this.scheduleClear(150);
      return;
    }

    this.scheduleClear(2000);
  }

  /**
   * Handle concierge complete
   */
  private handleComplete(courseId: number, result: string, status: string): void {
    logger.debug(`[CommisProgress] Complete: ${courseId} (${status})`);
    this.setState({ conciergeDone: true, reconnecting: false });
    this.maybeScheduleClear();
  }

  /**
   * Handle concierge deferred
   */
  private handleDeferred(): void {
    logger.debug('[CommisProgress] Deferred');
    this.setState({ conciergeDone: true, reconnecting: false });
    this.maybeScheduleClear();
  }

  /**
   * Handle error
   */
  private handleError(message: string): void {
    logger.error(`[CommisProgress] Error: ${message}`);
    this.scheduleClear(3000);
  }
}

// Export singleton instance
export const commisProgressStore = new CommisProgressStore();
