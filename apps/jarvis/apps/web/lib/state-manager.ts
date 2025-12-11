/**
 * State Manager Module
 * Centralizes global state management for Jarvis PWA
 */

import type { RealtimeSession } from '@openai/agents/realtime';
import type { SessionManager } from '@jarvis/core';
import type { VoiceAgentConfig } from '../contexts/types';

/**
 * Voice/connection status for React UI
 */
export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error';

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
}

/**
 * Global application state
 */
export interface AppState {
  // Core OpenAI SDK objects
  agent: any | null;
  session: RealtimeSession | null;
  sessionManager: SessionManager | null;

  // Conversation state
  currentStreamingText: string;
  currentConversationId: string | null;
  currentContext: any | null;

  // Jarvis-Zerg integration
  jarvisClient: any; // Type from @jarvis/core
  cachedAgents: any[];
  bootstrap: BootstrapData | null;

  // UI state
  statusActive: boolean;
  voiceStatus: VoiceStatus;

  // Media state
  sharedMicStream: MediaStream | null;
}

/**
 * State change event types
 */
export type StateChangeEvent =
  | { type: 'SESSION_CHANGED'; session: RealtimeSession | null }
  | { type: 'AGENT_CHANGED'; agent: any }
  | { type: 'CONTEXT_CHANGED'; context: any }
  | { type: 'STATUS_CHANGED'; active: boolean }
  | { type: 'STREAMING_TEXT_CHANGED'; text: string }
  | { type: 'CONVERSATION_ID_CHANGED'; id: string | null }
  | { type: 'VOICE_STATUS_CHANGED'; status: VoiceStatus }
  | { type: 'CONNECTION_ERROR'; error: Error }
  | { type: 'TOAST'; message: string; variant: 'success' | 'error' | 'info' }
  | { type: 'MESSAGE_FINALIZED'; message: { id: string; role: 'assistant'; content: string; timestamp: Date } }
  | { type: 'USER_VOICE_COMMITTED'; itemId: string }
  | { type: 'USER_VOICE_TRANSCRIPT'; itemId: string; transcript: string }
  | { type: 'HISTORY_LOADED'; history: any[] };

/**
 * State change listener
 */
export type StateChangeListener = (event: StateChangeEvent) => void;

/**
 * State Manager class
 */
export class StateManager {
  private state: AppState;
  private listeners: Set<StateChangeListener> = new Set();

  constructor() {
    this.state = this.createInitialState();
  }

  private createInitialState(): AppState {
    return {
      // Core objects
      agent: null,
      session: null,
      sessionManager: null,

      // Conversation state
      currentStreamingText: '',
      currentConversationId: null,
      currentContext: null,

      // Jarvis-Zerg integration
      jarvisClient: null,
      cachedAgents: [],
      bootstrap: null,

      // UI state
      statusActive: false,
      voiceStatus: 'idle',

      // Media
      sharedMicStream: null,
    };
  }

  /**
   * Get the current state
   */
  getState(): Readonly<AppState> {
    return { ...this.state };
  }

  /**
   * Update session
   */
  setSession(session: RealtimeSession | null): void {
    this.state.session = session;
    this.notifyListeners({ type: 'SESSION_CHANGED', session });
  }

  /**
   * Update agent
   */
  setAgent(agent: any | null): void {
    this.state.agent = agent;
    this.notifyListeners({ type: 'AGENT_CHANGED', agent });
  }

  /**
   * Update context
   */
  setContext(context: any | null): void {
    this.state.currentContext = context;
    this.notifyListeners({ type: 'CONTEXT_CHANGED', context });
  }

  /**
   * Update status active
   */
  setStatusActive(active: boolean): void {
    if (this.state.statusActive !== active) {
      this.state.statusActive = active;
      this.notifyListeners({ type: 'STATUS_CHANGED', active });
    }
  }

  /**
   * Update voice status (for React UI)
   */
  setVoiceStatus(status: VoiceStatus): void {
    if (this.state.voiceStatus !== status) {
      this.state.voiceStatus = status;
      this.notifyListeners({ type: 'VOICE_STATUS_CHANGED', status });
    }
  }

  /**
   * Show a toast notification (emits event for React to handle)
   */
  showToast(message: string, variant: 'success' | 'error' | 'info' = 'info'): void {
    this.notifyListeners({ type: 'TOAST', message, variant });
  }

  /**
   * Report connection error
   */
  setConnectionError(error: Error): void {
    this.notifyListeners({ type: 'CONNECTION_ERROR', error });
  }

  /**
   * Notify that a message has been finalized (streaming complete)
   */
  finalizeMessage(content: string): void {
    const message = {
      id: crypto.randomUUID(),
      role: 'assistant' as const,
      content,
      timestamp: new Date(),
      skipAnimation: true, // Skip fade-in since user already saw it streaming
    };
    this.notifyListeners({ type: 'MESSAGE_FINALIZED', message });
  }

  /**
   * Notify that user voice input was committed (placeholder should be shown)
   */
  userVoiceCommitted(itemId: string): void {
    this.notifyListeners({ type: 'USER_VOICE_COMMITTED', itemId });
  }

  /**
   * Notify that user voice transcript is ready (update placeholder content)
   */
  userVoiceTranscript(itemId: string, transcript: string): void {
    this.notifyListeners({ type: 'USER_VOICE_TRANSCRIPT', itemId, transcript });
  }

  /**
   * Notify that conversation history was loaded (for UI hydration)
   */
  historyLoaded(history: any[]): void {
    this.notifyListeners({ type: 'HISTORY_LOADED', history });
  }

  /**
   * Update streaming text
   */
  setStreamingText(text: string): void {
    this.state.currentStreamingText = text;
    this.notifyListeners({ type: 'STREAMING_TEXT_CHANGED', text });
  }

  /**
   * Update conversation ID
   */
  setConversationId(id: string | null): void {
    this.state.currentConversationId = id;
    this.notifyListeners({ type: 'CONVERSATION_ID_CHANGED', id });
  }

  /**
   * Update session manager
   */
  setSessionManager(manager: SessionManager | null): void {
    this.state.sessionManager = manager;
  }

  /**
   * Update Jarvis client
   */
  setJarvisClient(client: any): void {
    this.state.jarvisClient = client;
  }

  /**
   * Update cached agents
   */
  setCachedAgents(agents: any[]): void {
    this.state.cachedAgents = agents;
  }

  /**
   * Update shared mic stream
   */
  setSharedMicStream(stream: MediaStream | null): void {
    this.state.sharedMicStream = stream;
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
   * Get Jarvis client
   */
  getJarvisClient(): any {
    return this.state.jarvisClient;
  }

  /**
   * State check helpers
   */
  isConnected(): boolean {
    return this.state.session !== null;
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
