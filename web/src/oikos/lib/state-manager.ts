/**
 * State Manager Module
 * Small event bridge used by the legacy realtime lane and the chat controller.
 */

import { uuid } from './uuid';

/**
 * Model info from bootstrap
 */
export interface ModelInfo {
  id: string;
  display_name: string;
  description: string;
  capabilities?: { reasoning?: boolean; reasoningNone?: boolean };
}

/**
 * User preferences from bootstrap
 */
export interface ChatPreferences {
  chat_model: string;
  reasoning_effort: 'none' | 'low' | 'medium' | 'high';
}

/**
 * Bootstrap data from server
 */
export interface BootstrapData {
  prompt: string;
  enabled_tools: Array<{ name: string; description: string }>;
  user_context: {
    display_name?: string;
    role?: string;
    location?: string;
    servers?: Array<{ name: string; purpose: string }>;
  };
  available_models: ModelInfo[];
  preferences: ChatPreferences;
}

/**
 * Internal state retained outside React.
 */
interface StateManagerState {
  currentStreamingText: string;
  bootstrap: BootstrapData | null;
}

/**
 * State change event types
 */
export type StateChangeEvent =
  | { type: 'STREAMING_TEXT_CHANGED'; text: string }
  | { type: 'TOAST'; message: string; variant: 'success' | 'error' | 'info' }
  | { type: 'MESSAGE_FINALIZED'; message: { id: string; role: 'assistant'; content: string; timestamp: Date; skipAnimation?: boolean; messageId?: string } }
  | { type: 'ASSISTANT_STATUS_CHANGED_BY_MESSAGE_ID'; messageId: string; status: string; content?: string; usage?: { prompt_tokens?: number | null; completion_tokens?: number | null; total_tokens?: number | null; reasoning_tokens?: number | null }; runId?: number };

/**
 * State change listener
 */
export type StateChangeListener = (event: StateChangeEvent) => void;

/**
 * State Manager class
 */
export class StateManager {
  private state: StateManagerState;
  private listeners: Set<StateChangeListener> = new Set();

  constructor() {
    this.state = this.createInitialState();
  }

  private createInitialState(): StateManagerState {
    return {
      currentStreamingText: '',
      bootstrap: null,
    };
  }

  /**
   * Show a toast notification (emits event for React to handle)
   */
  showToast(message: string, variant: 'success' | 'error' | 'info' = 'info'): void {
    this.notifyListeners({ type: 'TOAST', message, variant });
  }

  /**
   * Notify that a message has been finalized (streaming complete)
   */
  finalizeMessage(content: string, messageId?: string): void {
    const message = {
      id: uuid(),
      role: 'assistant' as const,
      content,
      timestamp: new Date(),
      skipAnimation: true, // Skip fade-in since user already saw it streaming
      messageId,
    };
    this.notifyListeners({ type: 'MESSAGE_FINALIZED', message });
  }

  /**
   * Update assistant message status via messageId
   * This is the primary method for updating assistant messages during streaming.
   * For normal runs: messageId is client-generated upfront.
   * For continuation runs: messageId is backend-generated, received in oikos_started.
   */
  updateAssistantStatusByMessageId(
    messageId: string,
    status: string,
    content?: string,
    usage?: { prompt_tokens?: number | null; completion_tokens?: number | null; total_tokens?: number | null; reasoning_tokens?: number | null },
    runId?: number
  ): void {
    this.notifyListeners({ type: 'ASSISTANT_STATUS_CHANGED_BY_MESSAGE_ID', messageId, status, content, usage, runId });
  }

  /**
   * Update streaming text
   */
  setStreamingText(text: string): void {
    this.state.currentStreamingText = text;
    this.notifyListeners({ type: 'STREAMING_TEXT_CHANGED', text });
  }

  /**
   * Update bootstrap data
   */
  setBootstrap(data: BootstrapData | null): void {
    this.state.bootstrap = data;
  }

  /**
   * Get bootstrap data
   */
  getBootstrap(): BootstrapData | null {
    return this.state.bootstrap;
  }

  /**
   * Add state change listener
   */
  addListener(listener: StateChangeListener): void {
    this.listeners.add(listener);
  }

  /**
   * Remove state change listener
   */
  removeListener(listener: StateChangeListener): void {
    this.listeners.delete(listener);
  }

  /**
   * Notify all listeners of state change
   */
  private notifyListeners(event: StateChangeEvent): void {
    this.listeners.forEach(listener => listener(event));
  }

  /**
   * Reset state to initial
   */
  reset(): void {
    this.state = this.createInitialState();
  }
}

// Export singleton instance
export const stateManager = new StateManager();
