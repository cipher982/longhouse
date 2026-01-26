/**
 * Oikos Tool Store
 *
 * Tracks oikos tool calls as first-class conversation artifacts.
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

// Nested tool from commis execution
export interface NestedToolCall {
  toolCallId: string;
  toolName: string;
  status: ToolStatus;
  argsPreview?: string;
  startedAt: number;
  durationMs?: number;
  resultPreview?: string;
  error?: string;
}

// Result structure for spawn_commis tools
export interface SpawnCommisResult {
  commisStatus: 'spawned' | 'running' | 'complete' | 'failed';
  commisSummary?: string;
  nestedTools: NestedToolCall[];
  liveOutput?: string;
  liveOutputUpdatedAt?: number;
  rawResult?: unknown;
}

export interface OikosToolCall {
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

export interface OikosToolState {
  isActive: boolean;
  currentRunId: number | null;
  tools: Map<string, OikosToolCall>;  // keyed by toolCallId
  deferredRuns: Set<number>;  // runIds that have gone DEFERRED
}

type Listener = () => void;

/**
 * Oikos tool store for React integration via useSyncExternalStore
 */
class OikosToolStore {
  private static readonly MAX_LIVE_OUTPUT_CHARS = 50_000;
  private state: OikosToolState = {
    isActive: false,
    currentRunId: null,
    tools: new Map(),
    deferredRuns: new Set(),
  };

  private listeners = new Set<Listener>();
  private tickerInterval: number | null = null;
  private clearTimeout: number | null = null;

  // Map jobId -> toolCallId for spawn_commis tools
  private commisJobToToolCallId = new Map<number, string>();
  // Map commisId -> toolCallId for spawn_commis tools
  private commisIdToToolCallId = new Map<string, string>();

  constructor() {
    this.subscribeToEvents();
  }

  /**
   * Safely get spawn_commis result with proper typing
   */
  private getSpawnCommisResult(tool: OikosToolCall): SpawnCommisResult {
    const result = tool.result as SpawnCommisResult | undefined;
    return result ?? { commisStatus: 'spawned', nestedTools: [] };
  }

  /**
   * Get current state (for React)
   */
  getState(): OikosToolState {
    return this.state;
  }

  /**
   * Get tools for a specific run (for conversation display)
   */
  getToolsForRun(runId: number): OikosToolCall[] {
    return Array.from(this.state.tools.values())
      .filter(tool => tool.runId === runId)
      .sort((a, b) => a.startedAt - b.startedAt);
  }

  /**
   * Check if a run has been deferred (commiss continuing in background)
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
  private setState(updates: Partial<OikosToolState>): void {
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
        // Force new state reference so useSyncExternalStore detects the "change"
        // Without this, getState() returns the same object and React won't re-render
        this.state = { ...this.state };
        this.notifyListeners();
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
    // Oikos started - prepare for tool events
    eventBus.on('oikos:started', (data) => {
      this.cancelClearTimeout();
      this.setState({
        isActive: true,
        currentRunId: data.runId,
        // Don't clear tools - they persist across runs for conversation display
      });
      logger.debug('[OikosToolStore] Oikos started, run:', data.runId);
    });

    // Tool started
    eventBus.on('oikos:tool_started', (data) => {
      const newTools = new Map(this.state.tools);

      const tool: OikosToolCall = {
        toolCallId: data.toolCallId,
        toolName: data.toolName,
        status: 'running',
        runId: data.runId,
        startedAt: data.timestamp,
        argsPreview: data.argsPreview,
        args: data.args,
        logs: [],
      };

      // Initialize commis metadata for spawn_commis tools
      if (data.toolName === 'spawn_commis') {
        tool.result = {
          commisStatus: 'spawned',
          nestedTools: [],
        };
      }

      newTools.set(data.toolCallId, tool);

      this.setState({
        isActive: true,
        tools: newTools,
      });

      this.startTicker();
      logger.debug('[OikosToolStore] Tool started:', data.toolName);
    });

    // Tool progress (streaming logs)
    eventBus.on('oikos:tool_progress', (data) => {
      const newTools = new Map(this.state.tools);
      const tool = newTools.get(data.toolCallId);

      if (tool) {
        const logEntry: ToolLogEntry = {
          timestamp: data.timestamp,
          message: data.message,
          level: data.level || 'info',
          data: data.data,
        };

        const updatedTool: OikosToolCall = {
          ...tool,
          logs: [...tool.logs, logEntry],
        };

        newTools.set(data.toolCallId, updatedTool);
        this.setState({ tools: newTools });
      }

      logger.debug('[OikosToolStore] Tool progress:', data.message);
    });

    // Tool completed
    eventBus.on('oikos:tool_completed', (data) => {
      const newTools = new Map(this.state.tools);
      const tool = newTools.get(data.toolCallId);

      if (tool) {
        // For spawn_commis, merge result with existing commis metadata (commisStatus, nestedTools)
        // For other tools, just set result directly
        let mergedResult: Record<string, unknown> | undefined;
        if (tool.toolName === 'spawn_commis') {
          const existingResult = this.getSpawnCommisResult(tool);
          if (typeof data.result === 'object' && data.result !== null) {
            mergedResult = { ...existingResult, ...(data.result as Record<string, unknown>) };
          } else {
            mergedResult = { ...existingResult, rawResult: data.result };
          }
        } else {
          mergedResult = data.result;
        }

        const updatedTool: OikosToolCall = {
          ...tool,
          status: 'completed',
          completedAt: data.timestamp,
          durationMs: data.durationMs,
          resultPreview: data.resultPreview,
          result: mergedResult,
        };

        newTools.set(data.toolCallId, updatedTool);
        this.setState({ tools: newTools });

        // Extract job_id from spawn_commis result and update mapping
        if (tool.toolName === 'spawn_commis' && data.result != null) {
          const jobId = this.extractJobIdFromResult(data.result);
          if (jobId != null) {
            this.commisJobToToolCallId.set(jobId, data.toolCallId);
            logger.debug(`[OikosToolStore] Mapped job_id ${jobId} to tool ${data.toolCallId}`);
          }
        }
      }

      this.checkAndStopTicker();
      logger.debug(`[OikosToolStore] Tool completed: ${data.toolName} (${data.durationMs}ms)`);
    });

    // Tool failed
    eventBus.on('oikos:tool_failed', (data) => {
      const newTools = new Map(this.state.tools);
      const tool = newTools.get(data.toolCallId);

      if (tool) {
        const updatedTool: OikosToolCall = {
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
      logger.warn(`[OikosToolStore] Tool failed: ${data.toolName} - ${data.error}`);
    });

    // Oikos complete - schedule deactivation (but keep tools)
    eventBus.on('oikos:complete', () => {
      this.stopTicker();
      // Keep isActive true briefly so UI doesn't flicker
      this.clearTimeout = window.setTimeout(() => {
        this.setState({ isActive: false });
      }, 500);
      logger.debug('[OikosToolStore] Oikos complete');
    });

    // Oikos deferred - mark run as deferred (commiss continue in background)
    eventBus.on('oikos:deferred', (data) => {
      const newDeferredRuns = new Set(this.state.deferredRuns);
      newDeferredRuns.add(data.runId);
      this.setState({ deferredRuns: newDeferredRuns });
      logger.debug(`[OikosToolStore] Run ${data.runId} deferred`);
    });

    // Oikos error
    eventBus.on('oikos:error', () => {
      this.stopTicker();
      this.setState({ isActive: false });
      logger.debug('[OikosToolStore] Oikos error');
    });

    // Oikos cleared - reset active state
    eventBus.on('oikos:cleared', () => {
      this.stopTicker();
      this.cancelClearTimeout();
      this.setState({
        isActive: false,
        currentRunId: null,
      });
      logger.debug('[OikosToolStore] Oikos cleared');
    });

    // Commis lifecycle events - update spawn_commis tool metadata
    eventBus.on('oikos:commis_spawned', (data) => {
      // Use tool_call_id from event payload if available (parallel path includes it)
      // Fall back to finding most recent running tool (legacy single-commis path)
      let toolCallId = data.toolCallId || this.findMostRecentSpawnCommisTool();

      // If still not found, try searching by job_id in completed tools
      if (!toolCallId) {
        toolCallId = this.findSpawnCommisToolForJob(data.jobId);
      }

      if (toolCallId) {
        this.commisJobToToolCallId.set(data.jobId, toolCallId);
        this.updateCommisMetadata(toolCallId, { commisStatus: 'spawned' });
        logger.debug(`[OikosToolStore] Commis spawned for tool ${toolCallId} (job ${data.jobId})`);
      } else {
        logger.warn(`[OikosToolStore] Could not find spawn_commis tool for job ${data.jobId}`);
      }
    });

    eventBus.on('oikos:commis_started', (data) => {
      const toolCallId = this.commisJobToToolCallId.get(data.jobId);
      if (toolCallId) {
        if (data.commisId) {
          this.commisIdToToolCallId.set(data.commisId, toolCallId);
        } else {
          logger.warn(`[OikosToolStore] commis_started for job ${data.jobId} missing commisId`);
        }
        this.updateCommisMetadata(toolCallId, { commisStatus: 'running' });
        logger.debug(`[OikosToolStore] Commis started for tool ${toolCallId}`);
      }
    });

    eventBus.on('oikos:commis_complete', (data) => {
      const toolCallId = this.commisJobToToolCallId.get(data.jobId);
      if (toolCallId) {
        const status = data.status === 'success' ? 'complete' : 'failed';
        this.updateCommisMetadata(toolCallId, { commisStatus: status });
        logger.debug(`[OikosToolStore] Commis ${status} for tool ${toolCallId}`);
      }
    });

    eventBus.on('oikos:commis_summary', (data) => {
      const toolCallId = this.commisJobToToolCallId.get(data.jobId);
      if (toolCallId) {
        this.updateCommisMetadata(toolCallId, { commisSummary: data.summary });
        logger.debug(`[OikosToolStore] Commis summary for tool ${toolCallId}`);
      }
    });

    // Commis tool events - add to nested tools list
    eventBus.on('commis:tool_started', (data) => {
      const toolCallId = this.commisIdToToolCallId.get(data.commisId);

      if (toolCallId) {
        this.addNestedTool(toolCallId, {
          toolCallId: data.toolCallId,
          toolName: data.toolName,
          status: 'running',
          argsPreview: data.argsPreview,
          startedAt: data.timestamp,
        });
        logger.debug(`[OikosToolStore] Nested tool started: ${data.toolName}`);
      } else {
        // Commis tool events require commisId mapping from commis_started
        logger.warn(`[OikosToolStore] Could not route nested tool ${data.toolName} (commisId=${data.commisId} not found)`);
      }
    });

    eventBus.on('commis:tool_completed', (data) => {
      const toolCallId = this.commisIdToToolCallId.get(data.commisId);

      if (toolCallId) {
        this.updateNestedTool(toolCallId, data.toolCallId, {
          status: 'completed',
          durationMs: data.durationMs,
          resultPreview: data.resultPreview,
        });
        logger.debug(`[OikosToolStore] Nested tool completed: ${data.toolName}`);
      }
    });

    eventBus.on('commis:tool_failed', (data) => {
      const toolCallId = this.commisIdToToolCallId.get(data.commisId);

      if (toolCallId) {
        this.updateNestedTool(toolCallId, data.toolCallId, {
          status: 'failed',
          error: data.error,
          durationMs: data.durationMs,
        });
        logger.debug(`[OikosToolStore] Nested tool failed: ${data.toolName}`);
      }
    });

    eventBus.on('commis:output_chunk', (data) => {
      const toolCallId = data.commisId
        ? this.commisIdToToolCallId.get(data.commisId)
        : data.jobId
          ? this.commisJobToToolCallId.get(data.jobId)
          : undefined;

      if (toolCallId) {
        this.appendCommisLiveOutput(toolCallId, data.stream, data.data);
      } else {
        logger.warn('[OikosToolStore] Could not route commis output chunk (missing mapping)');
      }
    });
  }

  /**
   * Check if there's any active work (running tools, active commiss, or nested tools)
   */
  private hasActiveWork(): boolean {
    return Array.from(this.state.tools.values()).some(tool => {
      // Regular tool still running
      if (tool.status === 'running') return true;

      // spawn_commis with active commis or nested tools
      if (tool.toolName === 'spawn_commis') {
        const spawnResult = this.getSpawnCommisResult(tool);
        const { commisStatus, nestedTools } = spawnResult;

        // Commis is spawned or running
        if (commisStatus === 'spawned' || commisStatus === 'running') return true;

        // Has running nested tools
        if (nestedTools.some(nt => nt.status === 'running')) return true;
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
   * Find the most recent spawn_commis tool that's still running
   * Used when commis_spawned fires (before we have job_id in result)
   */
  private findMostRecentSpawnCommisTool(): string | null {
    let mostRecent: { toolCallId: string; startedAt: number } | null = null;

    for (const [toolCallId, tool] of this.state.tools.entries()) {
      if (tool.toolName === 'spawn_commis' && tool.status === 'running') {
        if (!mostRecent || tool.startedAt > mostRecent.startedAt) {
          mostRecent = { toolCallId, startedAt: tool.startedAt };
        }
      }
    }

    return mostRecent ? mostRecent.toolCallId : null;
  }

  /**
   * Find the spawn_commis tool for a given job ID (by checking existing mapping)
   */
  private findSpawnCommisToolForJob(jobId: number): string | null {
    // First check if we already have a mapping
    const existingMapping = this.commisJobToToolCallId.get(jobId);
    if (existingMapping) {
      return existingMapping;
    }

    // Fallback: search tools for job_id in result (set after tool_completed)
    for (const [toolCallId, tool] of this.state.tools.entries()) {
      if (tool.toolName === 'spawn_commis' && tool.result) {
        const resultJobId = this.extractJobIdFromResult(tool.result);
        if (resultJobId === jobId) {
          return toolCallId;
        }
      }
    }
    return null;
  }

  /**
   * Extract job_id from spawn_commis tool result
   * Result format: "Commis job {jobId} queued successfully..."
   */
  private extractJobIdFromResult(result: unknown): number | null {
    if (typeof result === 'string') {
      // Parse "Commis job 123 queued successfully..."
      const match = result.match(/Commis job (\d+)/);
      if (match) {
        return parseInt(match[1], 10);
      }
    } else if (result && typeof result === 'object' && 'job_id' in result) {
      // Handle structured result (if backend changes format)
      return (result as { job_id: number }).job_id;
    }
    return null;
  }

  /**
   * Update commis metadata for a spawn_commis tool
   */
  private updateCommisMetadata(toolCallId: string, metadata: Partial<SpawnCommisResult>): void {
    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_commis') {
      const existingResult = this.getSpawnCommisResult(tool);
      const updatedTool: OikosToolCall = {
        ...tool,
        result: {
          ...existingResult,
          ...metadata,
        },
      };
      newTools.set(toolCallId, updatedTool);
      this.setState({ tools: newTools });
    }
  }

  private appendCommisLiveOutput(toolCallId: string, stream: 'stdout' | 'stderr', data: string): void {
    if (!data) return;

    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_commis') {
      const existingResult = this.getSpawnCommisResult(tool);
      const prefix = stream === 'stderr' ? '[stderr] ' : '';
      let liveOutput = `${existingResult.liveOutput ?? ''}${prefix}${data}`;

      if (liveOutput.length > OikosToolStore.MAX_LIVE_OUTPUT_CHARS) {
        liveOutput = liveOutput.slice(-OikosToolStore.MAX_LIVE_OUTPUT_CHARS);
      }

      const updatedTool: OikosToolCall = {
        ...tool,
        result: {
          ...existingResult,
          liveOutput,
          liveOutputUpdatedAt: Date.now(),
        },
      };
      newTools.set(toolCallId, updatedTool);
      this.setState({ tools: newTools });
    }
  }

  /**
   * Add a nested tool to a spawn_commis tool
   */
  private addNestedTool(toolCallId: string, nestedTool: NestedToolCall): void {
    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_commis') {
      const existingResult = this.getSpawnCommisResult(tool);
      const updatedTool: OikosToolCall = {
        ...tool,
        result: {
          ...existingResult,
          nestedTools: [...existingResult.nestedTools, nestedTool],
        },
      };
      newTools.set(toolCallId, updatedTool);
      this.setState({ tools: newTools });
    }
  }

  /**
   * Update a nested tool within a spawn_commis tool
   */
  private updateNestedTool(toolCallId: string, nestedToolCallId: string, updates: Partial<NestedToolCall>): void {
    const newTools = new Map(this.state.tools);
    const tool = newTools.get(toolCallId);

    if (tool && tool.toolName === 'spawn_commis') {
      const existingResult = this.getSpawnCommisResult(tool);
      const updatedNested = existingResult.nestedTools.map(nt =>
        nt.toolCallId === nestedToolCallId ? { ...nt, ...updates } : nt
      );
      const updatedTool: OikosToolCall = {
        ...tool,
        result: {
          ...existingResult,
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
    this.commisJobToToolCallId.clear();
    this.commisIdToToolCallId.clear();
    this.setState({
      isActive: false,
      currentRunId: null,
      tools: new Map(),
      deferredRuns: new Set(),
    });
    logger.debug('[OikosToolStore] Tools cleared');
  }

  /**
   * Future: load tools from persisted data (for thread reload)
   */
  loadTools(tools: OikosToolCall[]): void {
    const newTools = new Map<string, OikosToolCall>();
    for (const tool of tools) {
      newTools.set(tool.toolCallId, tool);
    }
    this.setState({ tools: newTools });
    logger.debug(`[OikosToolStore] Loaded ${tools.length} tools from history`);
  }
}

// Singleton instance
export const oikosToolStore = new OikosToolStore();
