/**
 * State Manager Module
 * Centralizes global state management for Jarvis PWA
 */

import type { RealtimeAgent, RealtimeSession } from '@openai/agents/realtime';
import type { SessionManager, JarvisAgentSummary } from '@jarvis/core';
import type { ConversationUI } from './conversation-ui';
import type { ConversationRenderer } from './conversation-renderer';
import type { TaskInbox } from './task-inbox';
import type { VoiceAgentConfig } from '../contexts/types';
import { VoiceButtonState } from './config';
import type { InteractionStateMachine } from './interaction-state-machine';
import type { VoiceChannelController } from './voice-channel-controller';
import type { TextChannelController } from './text-channel-controller';

/**
 * Global application state
 */
export interface AppState {
  // Core OpenAI SDK objects
  agent: RealtimeAgent | null;
  session: RealtimeSession | null;
  sessionManager: SessionManager | null;

  // UI components
  conversationUI: ConversationUI | null;
  conversationRenderer: ConversationRenderer | null;

  // Voice/Text separation controllers
  interactionStateMachine: InteractionStateMachine | null;
  voiceChannelController: VoiceChannelController | null;
  textChannelController: TextChannelController | null;

  // Conversation state
  currentStreamingText: string;
  currentConversationId: string | null;
  voiceButtonState: VoiceButtonState;
  currentContext: VoiceAgentConfig | null;

  // Jarvis-Zerg integration
  conversationMode: 'voice' | 'text';
  taskInbox: TaskInbox | null;
  jarvisClient: any; // Type from @jarvis/core
  cachedAgents: JarvisAgentSummary[];

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
  | { type: 'AGENT_CHANGED'; agent: RealtimeAgent | null }
  | { type: 'CONTEXT_CHANGED'; context: VoiceAgentConfig | null }
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

      // UI components
      conversationUI: null,
      conversationRenderer: null,

      // Controllers
      interactionStateMachine: null,
      voiceChannelController: null,
      textChannelController: null,

      // Conversation state
      currentStreamingText: '',
      currentConversationId: null,
      voiceButtonState: VoiceButtonState.READY,
      currentContext: null,

      // Jarvis-Zerg integration
      conversationMode: 'voice',
      taskInbox: null,
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
  setAgent(agent: RealtimeAgent | null): void {
    this.state.agent = agent;
    this.notifyListeners({ type: 'AGENT_CHANGED', agent });
  }

  /**
   * Update context
   */
  setContext(context: VoiceAgentConfig | null): void {
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
   * Update conversation UI
   */
  setConversationUI(ui: ConversationUI | null): void {
    this.state.conversationUI = ui;
  }

  /**
   * Update conversation renderer
   */
  setConversationRenderer(renderer: ConversationRenderer | null): void {
    this.state.conversationRenderer = renderer;
  }

  /**
   * Update controllers
   */
  setControllers(controllers: {
    interactionStateMachine: InteractionStateMachine;
    voiceChannelController: VoiceChannelController;
    textChannelController: TextChannelController;
  }): void {
    this.state.interactionStateMachine = controllers.interactionStateMachine;
    this.state.voiceChannelController = controllers.voiceChannelController;
    this.state.textChannelController = controllers.textChannelController;
  }

  /**
   * Update task inbox
   */
  setTaskInbox(inbox: TaskInbox | null): void {
    this.state.taskInbox = inbox;
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
  setCachedAgents(agents: JarvisAgentSummary[]): void {
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
   * State check helpers (Simplified)
   */
  isReady(): boolean {
    return this.state.voiceButtonState === VoiceButtonState.READY;
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