/**
 * EventBus - Typed event system for decoupled controller communication
 *
 * Allows controllers to emit events without direct dependencies on each other.
 * UI components subscribe to events to update their state.
 *
 * Usage:
 *   // Emit an event
 *   eventBus.emit('voice_channel:muted', { muted: true });
 *
 *   // Subscribe to an event
 *   const unsubscribe = eventBus.on('voice_channel:muted', (data) => {
 *     console.log('Voice muted:', data.muted);
 *   });
 *
 *   // Unsubscribe when done
 *   unsubscribe();
 */

// Define all possible events and their payloads
export interface EventMap {
  // Voice Channel Events
  'voice_channel:muted': { muted: boolean };
  'voice_channel:transcript': { transcript: string; isFinal: boolean };
  'voice_channel:speaking_started': { timestamp: number };
  'voice_channel:speaking_stopped': { timestamp: number };
  'voice_channel:error': { error: Error; message: string };
  'voice_channel:mic_ready': { stream: MediaStream };

  // Text Channel Events
  'text_channel:send': { text: string; timestamp: number };
  'text_channel:sent': { text: string; timestamp: number };
  'text_channel:error': { error: Error; message: string };
  'text_channel:sending': { text: string };

  // Interaction State Events
  'state:changed': {
    from: InteractionState;
    to: InteractionState;
    timestamp: number
  };

  // Connection Events
  'connection:connecting': { timestamp: number };
  'connection:connected': { timestamp: number };
  'connection:disconnected': { timestamp: number };
  'connection:error': { error: Error; message: string };

  // Oikos Progress Events
  'oikos:started': { runId: number; task: string; timestamp: number; traceId?: string };
  'oikos:thinking': { message: string; timestamp: number };
  'oikos:commis_spawned': { jobId: number; task: string; timestamp: number; toolCallId?: string; runId?: number };
  'oikos:commis_started': { jobId: number; commisId?: string; timestamp: number; runId?: number };
  'oikos:commis_complete': { jobId: number; commisId?: string; status: string; durationMs?: number; timestamp: number; runId?: number };
  'oikos:commis_summary': { jobId: number; commisId?: string; summary: string; timestamp: number; runId?: number };
  'oikos:complete': {
    runId: number;
    result: string;
    status: string;
    durationMs?: number;
    timestamp: number;
    traceId?: string;
    usage?: {
      prompt_tokens?: number | null;
      completion_tokens?: number | null;
      total_tokens?: number | null;
      reasoning_tokens?: number | null;
    };
  };
  'oikos:error': { message: string; details?: string; timestamp: number; traceId?: string; runId?: number };
  'oikos:deferred': { runId: number; message: string; attachUrl?: string; timestamp: number };
  'oikos:waiting': { runId: number; jobId?: number; message: string; timestamp: number };
  'oikos:resumed': { runId: number; timestamp: number };
  'oikos:cleared': { timestamp: number };

  // Commis Tool Events (Phase 2: Activity Ticker)
  'commis:tool_started': {
    commisId: string;
    toolName: string;
    toolCallId: string;
    argsPreview?: string;
    runId?: number;
    timestamp: number;
  };
  'commis:tool_completed': {
    commisId: string;
    toolName: string;
    toolCallId: string;
    durationMs: number;
    resultPreview?: string;
    runId?: number;
    timestamp: number;
  };
  'commis:tool_failed': {
    commisId: string;
    toolName: string;
    toolCallId: string;
    durationMs: number;
    error: string;
    runId?: number;
    timestamp: number;
  };
  'commis:output_chunk': {
    commisId: string;
    jobId?: number;
    runnerJobId?: string;
    stream: 'stdout' | 'stderr';
    data: string;
    timestamp: number;
  };

  // Oikos Tool Events (uniform treatment with commis tools)
  'oikos:tool_started': {
    runId: number;
    toolName: string;
    toolCallId: string;
    argsPreview?: string;
    args?: Record<string, unknown>;  // Full args for raw view
    timestamp: number;
  };
  'oikos:tool_progress': {
    runId: number;
    toolCallId: string;
    message: string;
    level?: 'debug' | 'info' | 'warn' | 'error';
    progressPct?: number;
    data?: Record<string, unknown>;
    timestamp: number;
  };
  'oikos:tool_completed': {
    runId: number;
    toolName: string;
    toolCallId: string;
    durationMs: number;
    resultPreview?: string;
    result?: Record<string, unknown>;  // Full result for raw view
    timestamp: number;
  };
  'oikos:tool_failed': {
    runId: number;
    toolName: string;
    toolCallId: string;
    durationMs: number;
    error: string;
    errorDetails?: Record<string, unknown>;
    timestamp: number;
  };

  // Session Picker Events
  'oikos:show_session_picker': {
    runId: number;
    filters?: {
      project?: string;
      query?: string;
      provider?: string;
    };
    traceId?: string;
    timestamp: number;
  };

  // Test Events (E2E ready signals - DEV mode only)
  // Note: Prefer sticky flags (window.__oikos.ready) over events for "ready" signals
  'test:chat_ready': { timestamp: number };
  // Placeholder for future use - not currently emitted
  // 'test:messages_loaded': { count: number; timestamp: number };
}

// Interaction state machine types
export type InteractionMode = 'voice' | 'text';

export interface VoiceInteractionState {
  mode: 'voice';
  handsFree: boolean;  // Is hands-free mode enabled?
}

export interface TextInteractionState {
  mode: 'text';
}

export type InteractionState = VoiceInteractionState | TextInteractionState;

// Event handler type
type EventHandler<K extends keyof EventMap> = (data: EventMap[K]) => void;

export class EventBus {
  private handlers: Map<keyof EventMap, Set<EventHandler<any>>> = new Map();
  private debugMode: boolean = false;

  /**
   * Enable debug logging for all events
   */
  setDebugMode(enabled: boolean): void {
    this.debugMode = enabled;
  }

  /**
   * Subscribe to an event
   * @param event The event name to subscribe to
   * @param handler The callback function to invoke when the event is emitted
   * @returns A function to unsubscribe from the event
   */
  on<K extends keyof EventMap>(
    event: K,
    handler: EventHandler<K>
  ): () => void {
    if (!this.handlers.has(event)) {
      this.handlers.set(event, new Set());
    }

    const handlers = this.handlers.get(event)!;
    handlers.add(handler);

    // Return unsubscribe function
    return () => {
      handlers.delete(handler);
      if (handlers.size === 0) {
        this.handlers.delete(event);
      }
    };
  }

  /**
   * Subscribe to an event (once only)
   * @param event The event name to subscribe to
   * @param handler The callback function to invoke when the event is emitted
   * @returns A function to unsubscribe from the event
   */
  once<K extends keyof EventMap>(
    event: K,
    handler: EventHandler<K>
  ): () => void {
    const wrappedHandler = (data: EventMap[K]) => {
      unsubscribe();
      handler(data);
    };

    const unsubscribe = this.on(event, wrappedHandler);
    return unsubscribe;
  }

  /**
   * Emit an event to all subscribers
   * @param event The event name to emit
   * @param data The event payload
   */
  emit<K extends keyof EventMap>(event: K, data: EventMap[K]): void {
    if (this.debugMode) {
      console.log(`[EventBus] ${String(event)}:`, data);
    }

    const handlers = this.handlers.get(event);
    if (!handlers || handlers.size === 0) {
      return;
    }

    // Call all handlers (in a try-catch to prevent one handler from breaking others)
    handlers.forEach(handler => {
      try {
        handler(data);
      } catch (error) {
        console.error(`[EventBus] Error in handler for ${String(event)}:`, error);
      }
    });
  }

  /**
   * Remove all handlers for a specific event
   * @param event The event name to clear
   */
  off<K extends keyof EventMap>(event: K): void {
    this.handlers.delete(event);
  }

  /**
   * Remove all event handlers
   */
  clear(): void {
    this.handlers.clear();
  }

  /**
   * Get the number of handlers for a specific event
   * @param event The event name to check
   * @returns The number of handlers registered for this event
   */
  listenerCount<K extends keyof EventMap>(event: K): number {
    const handlers = this.handlers.get(event);
    return handlers ? handlers.size : 0;
  }

  /**
   * Get all registered event names
   * @returns An array of all event names that have handlers
   */
  eventNames(): Array<keyof EventMap> {
    return Array.from(this.handlers.keys());
  }
}

// Export a singleton instance
export const eventBus = new EventBus();

// Enable debug mode only for verbose logging
if (import.meta.env?.DEV && typeof window !== 'undefined') {
  const params = new URLSearchParams(window.location.search);
  const logLevel = params.get('log');
  if (logLevel === 'verbose') {
    eventBus.setDebugMode(true);
  }
}

// Expose the bus for Playwright E2E injection (dev server only).
if (import.meta.env?.DEV && typeof window !== 'undefined') {
  const w = window as any;
  w.__oikos = w.__oikos || {};
  w.__oikos.eventBus = eventBus;
}
