/**
 * State Manager Module
 * Centralizes global state management for Jarvis PWA
 */

import type { RealtimeSession } from '@openai/agents/realtime';
import type { SessionManager } from '@jarvis/core';
import type { VoiceAgentConfig } from '../contexts/types';

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

  // UI state
  statusActive: boolean;

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
  | { type: 'CONVERSATION_ID_CHANGED'; id: string | null };

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

      // UI state
      statusActive: false,

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
