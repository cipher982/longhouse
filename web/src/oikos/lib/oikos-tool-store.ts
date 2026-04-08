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
}

type Listener = () => void;

/**
 * Oikos tool store for React integration via useSyncExternalStore
 */
class OikosToolStore {
  private state: OikosToolState = {
    isActive: false,
    currentRunId: null,
    tools: new Map(),
  };

  private listeners = new Set<Listener>();
  private tickerInterval: number | null = null;
  private clearTimeout: number | null = null;

  constructor() {
    this.subscribeToEvents();
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
   * Start ticker for duration updates
   */
  private startTicker(): void {
    if (this.tickerInterval != null) return;
    this.cancelClearTimeout();
    this.tickerInterval = window.setInterval(() => {
      this.notifyListeners();
    }, 1000);
  }

  /**
   * Stop duration ticker
   */
  private stopTicker(): void {
    if (this.tickerInterval != null) {
      window.clearInterval(this.tickerInterval);
      this.tickerInterval = null;
    }
  }

  private cancelClearTimeout(): void {
    if (this.clearTimeout != null) {
      window.clearTimeout(this.clearTimeout);
      this.clearTimeout = null;
    }
  }

  /**
   * Subscribe to SSE events from the event bus
   */
  private subscribeToEvents(): void {
    // Run started
    eventBus.on('oikos:started', (data) => {
      this.cancelClearTimeout();
      this.setState({
        isActive: true,
        currentRunId: data.runId,
      });
      logger.debug(`[OikosToolStore] Run started: ${data.runId}`);
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
        const updatedTool: OikosToolCall = {
          ...tool,
          status: 'completed',
          completedAt: data.timestamp,
          durationMs: data.durationMs,
          resultPreview: data.resultPreview,
          result: data.result,
        };

        newTools.set(data.toolCallId, updatedTool);
        this.setState({ tools: newTools });
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
  }

  /**
   * Check if there's any active work (running tools)
   */
  private hasActiveWork(): boolean {
    return Array.from(this.state.tools.values()).some(tool => tool.status === 'running');
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
   * Clear all tools (e.g., when switching threads)
   */
  clearTools(): void {
    this.stopTicker();
    this.cancelClearTimeout();
    this.setState({
      isActive: false,
      currentRunId: null,
      tools: new Map(),
    });
    logger.debug('[OikosToolStore] Tools cleared');
  }

  /**
   * Future: load tools from persisted data (for thread reload)
   */
  loadTools(tools: OikosToolCall[]): void {
    const newTools = new Map<string, OikosToolCall>(this.state.tools);
    let added = 0;
    for (const tool of tools) {
      if (!newTools.has(tool.toolCallId)) {
        newTools.set(tool.toolCallId, tool);
        added += 1;
      }
    }
    this.setState({ tools: newTools });
    logger.debug(`[OikosToolStore] Loaded ${tools.length} tools from history (added ${added})`);
  }
}

// Singleton instance
export const oikosToolStore = new OikosToolStore();
