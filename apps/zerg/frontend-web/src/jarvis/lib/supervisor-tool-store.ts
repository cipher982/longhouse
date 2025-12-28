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
      const hasRunningTools = Array.from(this.state.tools.values())
        .some(tool => tool.status === 'running');

      if (hasRunningTools) {
        this.notifyListeners(); // Trigger re-render for live duration
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
        const updatedTool: SupervisorToolCall = {
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
  }

  /**
   * Check if we should stop the ticker (no running tools)
   */
  private checkAndStopTicker(): void {
    const hasRunningTools = Array.from(this.state.tools.values())
      .some(tool => tool.status === 'running');

    if (!hasRunningTools) {
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
