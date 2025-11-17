/**
 * State Manager Module
 * Centralizes global state management for Jarvis PWA
 */

import type { RealtimeSession } from '@openai/agents/realtime';
import type { SessionManager } from '@jarvis/core';
import type { VoiceAgentConfig } from '../contexts/types';
import { VoiceButtonState } from './config';

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
  voiceButtonState: VoiceButtonState;
  currentContext: any | null;

  // Jarvis-Zerg integration
  conversationMode: 'voice' | 'text';
  jarvisClient: any; // Type from @jarvis/core
  cachedAgents: any[];

  // UI state
  statusActive: boolean;
  pendingUserText: string;

  // Media state
  sharedMicStream: MediaStream | null;
}

/**
 * State change event types
 */
export type StateChangeEvent =
  | { type: 'VOICE_BUTTON_STATE_CHANGED'; state: VoiceButtonState }
  | { type: 'CONVERSATION_MODE_CHANGED'; mode: 'voice' | 'text' }
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
      voiceButtonState: VoiceButtonState.IDLE,
      currentContext: null,

      // Jarvis-Zerg integration
      conversationMode: 'voice',
      jarvisClient: null,
      cachedAgents: [],

      // UI state
      statusActive: false,
      pendingUserText: '',

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
   * Update voice button state
   */
  setVoiceButtonState(state: VoiceButtonState): void {
    if (this.state.voiceButtonState !== state) {
      this.state.voiceButtonState = state;
      this.notifyListeners({ type: 'VOICE_BUTTON_STATE_CHANGED', state });
    }
  }

  /**
   * Update conversation mode
   */
  setConversationMode(mode: 'voice' | 'text'): void {
    if (this.state.conversationMode !== mode) {
      this.state.conversationMode = mode;
      this.notifyListeners({ type: 'CONVERSATION_MODE_CHANGED', mode });
    }
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
   * Update pending user text
   */
  setPendingUserText(text: string): void {
    this.state.pendingUserText = text;
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
  isIdle(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.IDLE;
  }

  isConnecting(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.CONNECTING;
  }

  isReady(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.READY;
  }

  isSpeaking(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.SPEAKING;
  }

  isResponding(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.RESPONDING;
  }

  isActive(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.ACTIVE;
  }

  isProcessing(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.PROCESSING;
  }

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
