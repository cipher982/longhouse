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

// Subscription management
interface Subscription {
  unsubscribe: () => void;
}

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

// Enable debug mode in development
if (import.meta.env?.DEV) {
  eventBus.setDebugMode(true);
}
