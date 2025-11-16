/**
 * Session Handler Module
 * Manages OpenAI Realtime session lifecycle, connection, and reconnection
 */

import { RealtimeAgent, RealtimeSession, OpenAIRealtimeWebRTC } from '@openai/agents/realtime';
import { SessionManager, logger, getJarvisClient } from '@jarvis/core';
import type { VoiceAgentConfig } from '../contexts/types';
import { buildConversationManagerOptions, CONFIG, VoiceButtonState, getZergApiUrl } from './config';
import { stateManager } from './state-manager';
import { feedbackSystem } from './feedback-system';
import type { ConversationUI } from './conversation-ui';
import type { ConversationRenderer } from './conversation-renderer';

/**
 * Session handler configuration
 */
export interface SessionHandlerConfig {
  onSessionReady?: (session: RealtimeSession) => void;
  onSessionError?: (error: Error) => void;
  onSessionEnded?: () => void;
  onAgentReady?: (agent: RealtimeAgent) => void;
}

/**
 * Session Handler class
 */
export class SessionHandler {
  private config: SessionHandlerConfig;
  private reconnectTimeout?: NodeJS.Timeout;
  private isDestroying = false;

  constructor(config: SessionHandlerConfig = {}) {
    this.config = config;
  }

  /**
   * Create session manager for context
   */
  createSessionManagerForContext(config: VoiceAgentConfig): SessionManager {
    return new SessionManager({}, {
      conversationManagerOptions: buildConversationManagerOptions(config),
      maxHistoryTurns: config.settings.maxHistoryTurns
    });
  }

  /**
   * Initialize and connect session
   */
  async connect(context: VoiceAgentConfig): Promise<void> {
    const state = stateManager.getState();

    // Prevent multiple connections
    if (state.session || state.voiceButtonState === VoiceButtonState.CONNECTING) {
      logger.info('Session already exists or connecting, skipping connection');
      return;
    }

    try {
      this.isDestroying = false;
      stateManager.setVoiceButtonState(VoiceButtonState.CONNECTING);
      stateManager.setContext(context);

      // Update status in UI
      this.updateStatus('Connecting…');

      // Create session manager
      const sessionManager = this.createSessionManagerForContext(context);
      stateManager.setSessionManager(sessionManager);

      // Initialize conversation UI if needed
      if (!state.conversationUI) {
        await this.initializeConversationUI(sessionManager);
      }

      // Discover agent
      const jarvisClient = getJarvisClient(getZergApiUrl());
      stateManager.setJarvisClient(jarvisClient);

      const agents = await jarvisClient.listAgents();
      stateManager.setCachedAgents(agents);

      const agentName = context.agentName;
      const agent = agents.find(a => a.name === agentName);

      if (!agent) {
        throw new Error(`Agent "${agentName}" not found`);
      }

      logger.info('✅ Agent found:', agent.name || 'unnamed agent');

      // Request microphone if needed
      await this.ensureMicrophoneAccess();

      // Create agent with tools
      const realtimeAgent = await this.createAgent(context, agent);
      stateManager.setAgent(realtimeAgent);

      // Create and configure session
      const session = await this.createSession(realtimeAgent, context);
      stateManager.setSession(session);

      // Setup event handlers
      this.setupSessionEventHandlers(session);

      // Connect
      await this.performConnection(session, sessionManager);

      // Success feedback
      feedbackSystem.onConnect();
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
      this.updateStatus('Connected');

      // Notify callback
      this.config.onSessionReady?.(session);

    } catch (error) {
      logger.error('Failed to connect:', error);
      this.handleConnectionError(error as Error);
    }
  }

  /**
   * Disconnect and cleanup session
   */
  async disconnect(): Promise<void> {
    this.isDestroying = true;
    this.clearReconnectTimeout();

    const state = stateManager.getState();

    try {
      // Disconnect session
      if (state.session) {
        await state.session.disconnect();
      }

      // Cleanup media
      if (state.sharedMicStream) {
        state.sharedMicStream.getTracks().forEach(track => track.stop());
        stateManager.setSharedMicStream(null);
      }

      // Reset state
      stateManager.setSession(null);
      stateManager.setAgent(null);
      stateManager.setVoiceButtonState(VoiceButtonState.IDLE);

      // Feedback
      feedbackSystem.onDisconnect();
      this.updateStatus('Disconnected');

      // Notify callback
      this.config.onSessionEnded?.();

    } catch (error) {
      logger.error('Error during disconnect:', error);
    }
  }

  /**
   * Handle reconnection
   */
  async reconnect(): Promise<void> {
    const context = stateManager.getState().currentContext;
    if (!context) {
      logger.error('No context available for reconnection');
      return;
    }

    await this.disconnect();

    // Wait before reconnecting
    await new Promise(resolve => setTimeout(resolve, CONFIG.VOICE.RECONNECT_DELAY_MS));

    if (!this.isDestroying) {
      await this.connect(context);
    }
  }

  /**
   * Ensure microphone access
   */
  private async ensureMicrophoneAccess(): Promise<void> {
    const state = stateManager.getState();

    if (!state.sharedMicStream) {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          sampleRate: 24000
        }
      });
      stateManager.setSharedMicStream(stream);
    }
  }

  /**
   * Initialize conversation UI
   */
  private async initializeConversationUI(sessionManager: SessionManager): Promise<void> {
    const { ConversationUI } = await import('./conversation-ui');
    const conversationUI = new ConversationUI(sessionManager);
    stateManager.setConversationUI(conversationUI);
  }

  /**
   * Create agent with tools
   */
  private async createAgent(context: VoiceAgentConfig, agentInfo: any): Promise<RealtimeAgent> {
    // This would be expanded to include actual tool creation
    // For now, returning a basic agent
    const agent = new RealtimeAgent({
      apiKey: context.apiKey,
      model: context.model,
      systemMessage: context.systemMessage,
      temperature: context.temperature,
      maxResponseOutputTokens: context.maxResponseOutputTokens,
      voice: context.voice,
      audioFormat: context.audioFormat,
      instructions: context.instructions,
      tools: [] // Tools would be added here
    });

    this.config.onAgentReady?.(agent);
    return agent;
  }

  /**
   * Create and configure session
   */
  private async createSession(agent: RealtimeAgent, context: VoiceAgentConfig): Promise<RealtimeSession> {
    const Transport = context.apiKey ? OpenAIRealtimeWebRTC : OpenAIRealtimeWebRTC;
    const session = new RealtimeSession({
      agent,
      transport: new Transport({ apiKey: context.apiKey }),
      turnDetection: {
        type: context.turnDetection?.type || 'server_vad',
        threshold: context.turnDetection?.threshold,
        prefixPaddingMs: context.turnDetection?.prefixPaddingMs,
        silenceDurationMs: context.turnDetection?.silenceDurationMs
      },
      modalities: ['text', 'audio']
    });

    return session;
  }

  /**
   * Setup session event handlers
   */
  private setupSessionEventHandlers(session: RealtimeSession): void {
    session.on('error', (error) => {
      logger.error('Session error:', error);
      this.handleSessionError(error);
    });

    session.on('disconnected', () => {
      if (!this.isDestroying) {
        logger.info('Session disconnected unexpectedly');
        this.scheduleReconnect();
      }
    });
  }

  /**
   * Perform actual connection
   */
  private async performConnection(session: RealtimeSession, sessionManager: SessionManager): Promise<void> {
    // Get microphone stream
    const micStream = stateManager.getState().sharedMicStream;
    if (!micStream) {
      throw new Error('Microphone stream not available');
    }

    // Set audio stream
    session.setAudioStream(micStream);

    // Connect session
    await session.connect();

    // Initialize conversation
    const conversation = await sessionManager.createConversation();
    stateManager.setConversationId(conversation.id);
  }

  /**
   * Handle connection error
   */
  private handleConnectionError(error: Error): void {
    stateManager.setVoiceButtonState(VoiceButtonState.IDLE);
    feedbackSystem.onError();
    this.updateStatus(`Error: ${error.message}`);
    this.config.onSessionError?.(error);
  }

  /**
   * Handle session error
   */
  private handleSessionError(error: any): void {
    if (error.code === 'session_expired') {
      this.scheduleReconnect();
    } else {
      this.config.onSessionError?.(error);
    }
  }

  /**
   * Schedule reconnection
   */
  private scheduleReconnect(): void {
    if (this.isDestroying) return;

    this.clearReconnectTimeout();
    this.reconnectTimeout = setTimeout(() => {
      this.reconnect();
    }, CONFIG.VOICE.RECONNECT_DELAY_MS);
  }

  /**
   * Clear reconnect timeout
   */
  private clearReconnectTimeout(): void {
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = undefined;
    }
  }

  /**
   * Update status in UI
   */
  private updateStatus(message: string): void {
    const renderer = stateManager.getState().conversationRenderer;
    if (renderer) {
      renderer.updateStatus(message);
    }
  }

  /**
   * Cleanup
   */
  cleanup(): void {
    this.isDestroying = true;
    this.clearReconnectTimeout();
    this.disconnect();
  }
}

// Export singleton instance
export const sessionHandler = new SessionHandler();