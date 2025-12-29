/**
 * Supervisor Tool Store
 *
 * Tracks supervisor tool calls as first-class conversation artifacts.
 * These appear inline before the assistant response, providing:
 * - Real-time "productive theater" during execution
 * - Session-scoped record for the active conversation (DB persistence is a future phase)
 * - Progressive disclosure (collapsed → expanded → raw)
 *
 * Design principles:
 * - Uniform treatment: all tools get same UI frame + lifecycle
 * - Clear UX: tool calls are visible, ordered, and expandable
 * - High density: power users see what's happening
 */

import { eventBus } from './event-bus';
import { logger } from '../core';

export type ToolStatus = 'running' | 'completed' | 'failed';
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface ToolLogEntry {
  timestamp: number;
  message: string;
  level: LogLevel;
  data?: Record<string, unknown>;
}

export interface SupervisorToolCall {
  toolCallId: string;
  toolName: string;
  status: ToolStatus;
  runId: number;

  // Timing
  startedAt: number;
  completedAt?: number;
  durationMs?: number;

  // Arguments (for display and raw view)
  argsPreview?: string;
  args?: Record<string, unknown>;

  // Result (for display and raw view)
  resultPreview?: string;
  result?: Record<string, unknown>;

  // Error info
  error?: string;
  errorDetails?: Record<string, unknown>;

  // Progress logs (streaming)
  logs: ToolLogEntry[];
}

export interface SupervisorToolState {
  isActive: boolean;
  currentRunId: number | null;
  tools: Map<string, SupervisorToolCall>;  // keyed by toolCallId
  deferredRuns: Set<number>;  // runIds that have gone DEFERRED
}

type Listener = () => void;

/**
 * Supervisor tool store for React integration via useSyncExternalStore
 */
class SupervisorToolStore {
  private state: SupervisorToolState = {
    isActive: false,
    currentRunId: null,
    tools: new Map(),
    deferredRuns: new Set(),
  };

  private listeners = new Set<Listener>();
  private tickerInterval: number | null = null;
  private clearTimeout: number | null = null;

  // Map jobId -> toolCallId for spawn_worker tools
  private workerJobToToolCallId = new Map<number, string>();
  // Map workerId -> toolCallId for spawn_worker tools
  private workerIdToToolCallId = new Map<string, string>();

  constructor() {
    this.subscribeToEvents();
  }

  /**
   * Get current state (for React)
   */
  getState(): SupervisorToolState {
    return this.state;
  }

  /**
   * Get tools for a specific run (for conversation display)
   */
  getToolsForRun(runId: number): SupervisorToolCall[] {
    return Array.from(this.state.tools.values())
      .filter(tool => tool.runId === runId)
      .sort((a, b) => a.startedAt - b.startedAt);
  }

  /**
   * Check if a run has been deferred (workers continuing in background)
   */
  isDeferred(runId: number | null): boolean {
    return runId !== null && this.state.deferredRuns.has(runId);
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
  private setState(updates: Partial<SupervisorToolState>): void {
    this.state = { ...this.state, ...updates };
    this.notifyListeners();
  }

  /**
   * Start the ticker for live duration updates on running tools
   */
  private startTicker(): void {
    if (this.tickerInterval) return;

    this.tickerInterval = window.setInterval(() => {
      const hasRunningTools = this.hasActiveWork();

      if (hasRunningTools) {
        this.notifyListeners(); // Trigger re-render for live duration
      } else {
        // Stop ticker if no active work remains (handles deferred runs)
        this.stopTicker();
      }
    }, 500);
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
   * Clear scheduled clear timeout
   */
  private cancelClearTimeout(): void {
    if (this.clearTimeout) {
      clearTimeout(this.clearTimeout);
      this.clearTimeout = null;
    }
  }

  /**
   * Subscribe to EventBus events
   */
  private subscribeToEvents(): void {
    // Supervisor started - prepare for tool events
    eventBus.on('supervisor:started', (data) => {
      this.cancelClearTimeout();
      this.setState({
        isActive: true,
        currentRunId: data.runId,
        // Don't clear tools - they persist across runs for conversation display
      });
      logger.debug('[SupervisorToolStore] Supervisor started, run:', data.runId);
    });

    // Tool started
    eventBus.on('supervisor:tool_started', (data) => {
      const newTools = new Map(this.state.tools);

      const tool: SupervisorToolCall = {
        toolCallId: data.toolCallId,
        toolName: data.toolName,
        status: 'running',
        runId: data.runId,
        startedAt: data.timestamp,
        argsPreview: data.argsPreview,
        args: data.args,
        logs: [],
      };

      // Initialize worker metadata for spawn_worker tools
      if (data.toolName === 'spawn_worker') {
        tool.result = {
          workerStatus: 'spawned',
          nestedTools: [],
        };
      }

      newTools.set(data.toolCallId, tool);

      this.setState({
        isActive: true,
        tools: newTools,
      });

      this.startTicker();
      logger.debug('[SupervisorToolStore] Tool started:', data.toolName);
    });

    // Tool progress (streaming logs)
    eventBus.on('supervisor:tool_progress', (data) => {
      const newTools = new Map(this.state.tools);
      const tool = newTools.get(data.toolCallId);

      if (tool) {
        const logEntry: ToolLogEntry = {
          timestamp: data.timestamp,
          message: data.message,
          level: data.level || 'info',
          data: data.data,
        };

        const updatedTool: SupervisorToolCall = {
          ...tool,
          logs: [...tool.logs, logEntry],
        };

        newTools.set(data.toolCallId, updatedTool);
        this.setState({ tools: newTools });
      }

      logger.debug('[SupervisorToolStore] Tool progress:', data.message);
    });

    // Tool completed
    eventBus.on('supervisor:tool_completed', (data) => {
      const newTools = new Map(this.state.tools);
      const tool = newTools.get(data.toolCallId);

      if (tool) {
        // For spawn_worker, merge result with existing worker metadata (workerStatus, nestedTools)
        // For other tools, just set result directly
        const mergedResult = tool.toolName === 'spawn_worker' && typeof data.result === 'object' && data.result !== null
          ? { ...(tool.result as any), ...data.result }
          : tool.toolName === 'spawn_worker'
          ? { ...(tool.result as any), rawResult: data.result }
          : data.result;

        const updatedTool: SupervisorToolCall = {
          ...tool,
          status: 'completed',
          completedAt: data.timestamp,
          durationMs: data.durationMs,
          resultPreview: data.resultPreview,
          result: mergedResult,
        };

        newTools.set(data.toolCallId, updatedTool);
        this.setState({ tools: newTools });

        // Extract job_id from spawn_worker result and update mapping
        if (tool.toolName === 'spawn_worker' && data.result) {
          const jobId = this.extractJobIdFromResult(data.result);
          if (jobId) {
            this.workerJobToToolCallId.set(jobId, data.toolCallId);
            logger.debug(`[SupervisorToolStore] Mapped job_id ${jobId} to tool ${data.toolCallId}`);
          }
        }
      }

      this.checkAndStopTicker();
      logger.debug(`[SupervisorToolStore] Tool completed: ${data.toolName} (${data.durationMs}ms)`);
    });

    // Tool failed
    eventBus.on('supervisor:tool_failed', (data) => {
      const newTools = new Map(this.state.tools);
      const tool = newTools.get(data.toolCallId);

      if (tool) {
        const updatedTool: SupervisorToolCall = {
          ...tool,
          status: 'failed',
          completedAt: data.timestamp,
          durationMs: data.durationMs,
          error: data.error,
          errorDetails: data.errorDetails,
        };

        newTools.set(data.toolCallId, updatedTool);
        this.setState({ tools: newTools });
      }

      this.checkAndStopTicker();
      logger.warn(`[SupervisorToolStore] Tool failed: ${data.toolName} - ${data.error}`);
    });

    // Supervisor complete - schedule deactivation (but keep tools)
    eventBus.on('supervisor:complete', () => {
      this.stopTicker();
      // Keep isActive true briefly so UI doesn't flicker
      this.clearTimeout = window.setTimeout(() => {
        this.setState({ isActive: false });
      }, 500);
      logger.debug('[SupervisorToolStore] Supervisor complete');
    });

    // Supervisor deferred - mark run as deferred (workers continue in background)
    eventBus.on('supervisor:deferred', (data) => {
      const newDeferredRuns = new Set(this.state.deferredRuns);
      newDeferredRuns.add(data.runId);
      this.setState({ deferredRuns: newDeferredRuns });
      logger.debug(`[SupervisorToolStore] Run ${data.runId} deferred`);
    });

    // Supervisor error
    eventBus.on('supervisor:error', () => {
      this.stopTicker();
      this.setState({ isActive: false });
      logger.debug('[SupervisorToolStore] Supervisor error');
    });

    // Supervisor cleared - reset active state
    eventBus.on('supervisor:cleared', () => {
      this.stopTicker();
      this.cancelClearTimeout();
      this.setState({
        isActive: false,
        currentRunId: null,
      });
      logger.debug('[SupervisorToolStore] Supervisor cleared');
    });

    // Worker lifecycle events - update spawn_worker tool metadata
    eventBus.on('supervisor:worker_spawned', (data) => {
      // Find the spawn_worker tool for this job
      const toolCallId = this.findSpawnWorkerToolForJob(data.jobId);
      if (toolCallId) {
        this.workerJobToToolCallId.set(data.jobId, toolCallId);
        this.updateWorkerMetadata(toolCallId, { workerStatus: 'spawned' });
        logger.debug(`[SupervisorToolStore] Worker spawned for tool ${toolCallId}`);
      }
    });

    eventBus.on('supervisor:worker_started', (data) => {
      const toolCallId = this.workerJobToToolCallId.get(data.jobId);
      if (toolCallId) {
        if (data.workerId) {
          this.workerIdToToolCallId.set(data.workerId, toolCallId);
        } else {
          logger.warn(`[SupervisorToolStore] worker_started for job ${data.jobId} missing workerId`);
        }
        this.updateWorkerMetadata(toolCallId, { workerStatus: 'running' });
        logger.debug(`[SupervisorToolStore] Worker started for tool ${toolCallId}`);
      }
    });

    eventBus.on('supervisor:worker_complete', (data) => {
      const toolCallId = this.workerJobToToolCallId.get(data.jobId);
      if (toolCallId) {
        const status = data.status === 'success' ? 'complete' : 'failed';
        this.updateWorkerMetadata(toolCallId, { workerStatus: status });
        logger.debug(`[SupervisorToolStore] Worker ${status} for tool ${toolCallId}`);
      }
    });

    eventBus.on('supervisor:worker_summary', (data) => {
      const toolCallId = this.workerJobToToolCallId.get(data.jobId);
      if (toolCallId) {
        this.updateWorkerMetadata(toolCallId, { workerSummary: data.summary });
        logger.debug(`[SupervisorToolStore] Worker summary for tool ${toolCallId}`);
      }
    });

    // Worker tool events - add to nested tools list
    eventBus.on('worker:tool_started', (data) => {
      const toolCallId = this.workerIdToToolCallId.get(data.workerId);

      if (toolCallId) {
        this.addNestedTool(toolCallId, {
          toolCallId: data.toolCallId,
          toolName: data.toolName,
          status: 'running',
          argsPreview: data.argsPreview,
          startedAt: data.timestamp,
        });
        logger.debug(`[SupervisorToolStore] Nested tool started: ${data.toolName}`);
      } else {
        // Worker tool events require workerId mapping from worker_started
        logger.warn(`[SupervisorToolStore] Could not route nested tool ${data.toolName} (workerId=${data.workerId} not found)`);
      }
    });

    eventBus.on('worker:tool_completed', (data) => {
      const toolCallId = this.workerIdToToolCallId.get(data.workerId);

      if (toolCallId) {
        this.updateNestedTool(toolCallId, data.toolCallId, {
          status: 'completed',
          durationMs: data.durationMs,
        });
        logger.debug(`[SupervisorToolStore] Nested tool completed: ${data.toolName}`);
      }
    });

    eventBus.on('worker:tool_failed', (data) => {
      const toolCallId = this.workerIdToToolCallId.get(data.workerId);

      if (toolCallId) {
        this.updateNestedTool(toolCallId, data.toolCallId, {
          status: 'failed',
          error: data.error,
          durationMs: data.durationMs,
        });
        logger.debug(`[SupervisorToolStore] Nested tool failed: ${data.toolName}`);
      }
    });
  }

  /**
   * Check if there's any active work (running tools, active workers, or nested tools)
   */
  private hasActiveWork(): boolean {
    return Array.from(this.state.tools.values()).some(tool => {
      // Regular tool still running
      if (tool.status === 'running') return true;

      // spawn_worker with active worker or nested tools
      if (tool.toolName === 'spawn_worker') {
        const workerStatus = (tool.result as any)?.workerStatus;
        const nestedTools = (tool.result as any)?.nestedTools || [];

        // Worker is spawned or running
        if (workerStatus === 'spawned' || workerStatus === 'running') return true;

        // Has running nested tools
        if (nestedTools.some((nt: any) => nt.status === 'running')) return true;
      }

      return false;
    });
  }

  /**
   * Check if we should stop the ticker (no running tools)
   */
  private checkAndStopTicker(): void {
    if (!this.hasActiveWork()) {
      this.stopTicker();
    }
  }

  /**
   * Find the spawn_worker tool for a given job ID (by checking existing mapping)
   */
  private findSpawnWorkerToolForJob(jobId: number): string | null {
    // First check if we already have a mapping
    const existingMapping = this.workerJobToToolCallId.get(jobId);
    if (existingMapping) {
      return existingMapping;
    }

    // Fallback: search tools for job_id in result (set after tool_completed)
    for (const [toolCallId, tool] of this.state.tools.entries()) {
      if (tool.toolName === 'spawn_worker' && tool.result) {
        const resultJobId = this.extractJobIdFromResult(tool.result);
        if (resultJobId === jobId) {
          return toolCallId;
        }
      }
    }
    return null;
  }

  /**
   * Extract job_id from spawn_worker tool result
   * Result format: "Worker job {jobId} queued successfully..."
   */
  private extractJobIdFromResult(result: any): number | null {
    if (typeof result === 'string') {
      // Parse "Worker job 123 queued successfully..."
      const match = result.match(/Worker job (\d+)/);
      if (match) {
        return parseInt(match[1], 10);
      }
    } else if (result && typeof result === 'object' && 'job_id' in result) {
      // Handle structured result (if backend changes format)
      return result.job_id;
    }
    return null;
  }

  /**
   * Update worker metadata for a spawn_worker tool
   */
  private updateWorkerMetadata(toolCallId: string, metadata: Record<string, any>): void {
    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_worker') {
      const updatedTool: SupervisorToolCall = {
        ...tool,
        result: {
          ...(tool.result as any),
          ...metadata,
        },
      };
      newTools.set(toolCallId, updatedTool);
      this.setState({ tools: newTools });
    }
  }

  /**
   * Add a nested tool to a spawn_worker tool
   */
  private addNestedTool(toolCallId: string, nestedTool: any): void {
    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_worker') {
      const currentNested = ((tool.result as any)?.nestedTools || []) as any[];
      const updatedTool: SupervisorToolCall = {
        ...tool,
        result: {
          ...(tool.result as any),
          nestedTools: [...currentNested, nestedTool],
        },
      };
      newTools.set(toolCallId, updatedTool);
      this.setState({ tools: newTools });
    }
  }

  /**
   * Update a nested tool within a spawn_worker tool
   */
  private updateNestedTool(toolCallId: string, nestedToolCallId: string, updates: Record<string, any>): void {
    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_worker') {
      const nestedTools = ((tool.result as any)?.nestedTools || []) as any[];
      const updatedNested = nestedTools.map(nt =>
        nt.toolCallId === nestedToolCallId ? { ...nt, ...updates } : nt
      );
      const updatedTool: SupervisorToolCall = {
        ...tool,
        result: {
          ...(tool.result as any),
          nestedTools: updatedNested,
        },
      };
      newTools.set(toolCallId, updatedTool);
      this.setState({ tools: newTools });
    }
  }

  /**
   * Clear all tools (e.g., when switching threads)
   */
  clearTools(): void {
    this.stopTicker();
    this.cancelClearTimeout();
    this.workerJobToToolCallId.clear();
    this.workerIdToToolCallId.clear();
    this.setState({
      isActive: false,
      currentRunId: null,
      tools: new Map(),
      deferredRuns: new Set(),
    });
    logger.debug('[SupervisorToolStore] Tools cleared');
  }

  /**
   * Future: load tools from persisted data (for thread reload)
   */
  loadTools(tools: SupervisorToolCall[]): void {
    const newTools = new Map<string, SupervisorToolCall>();
    for (const tool of tools) {
      newTools.set(tool.toolCallId, tool);
    }
    this.setState({ tools: newTools });
    logger.debug(`[SupervisorToolStore] Loaded ${tools.length} tools from history`);
  }
}

// Singleton instance
export const supervisorToolStore = new SupervisorToolStore();
