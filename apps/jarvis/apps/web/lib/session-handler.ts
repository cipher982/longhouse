/**
 * Session Handler Module
 * Manages OpenAI Realtime session lifecycle
 */

import { RealtimeAgent, RealtimeSession, OpenAIRealtimeWebRTC } from '@openai/agents/realtime';
import { logger, getRealtimeModel } from '@jarvis/core';
import type { ConversationTurn } from '@jarvis/data-local';
import { VoiceButtonState, CONFIG } from './config';
import { mapConversationToRealtimeItems, trimForRealtime } from './history-mapper';
import type { VoiceAgentConfig } from '../contexts/types';

/**
 * Session connection options
 */
export interface SessionConnectionOptions {
  context: VoiceAgentConfig;
  mediaStream?: MediaStream;
  audioElement?: HTMLAudioElement;
  tools?: any[];
  onTokenRequest?: () => Promise<string>;
}

/**
 * Session connection options with pre-loaded history (SSOT pattern)
 * Used by bootstrapSession to ensure UI and Realtime use same data.
 */
export interface SessionConnectionWithHistoryOptions extends SessionConnectionOptions {
  /** Pre-loaded and mapped history items - do not re-query */
  historyItems: import('@openai/agents/realtime').RealtimeMessageItem[];
}

/**
 * Session handler configuration
 */
export interface SessionHandlerConfig {
  onSessionReady?: (session: RealtimeSession, agent: RealtimeAgent) => void;
  onSessionError?: (error: Error) => void;
  onSessionEnded?: () => void;
  onSessionEvent?: (event: string, data: any) => void;
}

/**
 * Standard session configuration for OpenAI Realtime
 *
 * IMPORTANT (v2.1 Architecture): Realtime is I/O only - transcription + VAD.
 * Supervisor is the "one brain" that generates all assistant responses.
 * Setting create_response=false prevents Realtime from auto-generating responses.
 */
const SESSION_CONFIG = {
  inputAudioTranscription: { model: 'whisper-1' as const },
  audio: {
    output: {
      voice: 'verse' as const,
      speed: 1.3  // 30% faster speech
    }
  },
  turnDetection: {
    type: 'server_vad' as const,
    threshold: 0.5,
    prefix_padding_ms: 300,
    silence_duration_ms: 1500,
    // v2.1: Prevent Realtime from generating responses - Supervisor is the only brain
    create_response: false
  }
};

/**
 * Session Handler class - wraps session creation and lifecycle
 */
export class SessionHandler {
  private config: SessionHandlerConfig = {};
  private reconnectTimeout?: NodeJS.Timeout;
  private isDestroying = false;
  private currentSession?: RealtimeSession;
  private currentAgent?: RealtimeAgent;

  /**
   * Set configuration
   */
  setConfig(config: SessionHandlerConfig): void {
    this.config = { ...this.config, ...config };
  }

  /**
   * Create agent, transport, and session (shared setup for all connect methods)
   */
  private createSession(options: SessionConnectionOptions): { agent: RealtimeAgent; session: RealtimeSession } {
    // Always create a new agent to ensure tools are fresh
    const agent = new RealtimeAgent({
      name: options.context.name,
      instructions: options.context.instructions,
      tools: options.tools || []
    });

    logger.info(`🤖 Created agent: ${options.context.name} with ${options.tools?.length || 0} tools`);

    // Create WebRTC transport
    // Note: We cannot modify iceTransportPolicy after the SDK creates the connection.
    // The "local network access" prompt in Chrome is a side effect of WebRTC's ICE
    // candidate gathering. Users will see this prompt when connecting.
    const transport = new OpenAIRealtimeWebRTC({
      mediaStream: options.mediaStream,
      audioElement: options.audioElement,
    });

    // Create the RealtimeSession with standard config
    const session = new RealtimeSession(agent, {
      transport,
      model: getRealtimeModel(),
      config: SESSION_CONFIG
    });

    // Store references
    this.currentSession = session;
    this.currentAgent = agent;

    return { agent, session };
  }

  /**
   * Authenticate and connect session
   */
  private async authenticateAndConnect(session: RealtimeSession, onTokenRequest?: () => Promise<string>): Promise<void> {
    if (!onTokenRequest) {
      throw new Error('No token request handler provided');
    }
    logger.info('🎫 Requesting session token...');
    const token = await onTokenRequest();
    logger.info('🔌 Connecting to OpenAI Realtime...');
    await session.connect({ apiKey: token });
  }

  /**
   * Create and connect a session with pre-loaded history (SSOT pattern)
   * This is the primary connection method - used by bootstrapSession().
   *
   * @param options - Connection options including pre-loaded historyItems
   */
  async connectWithHistory(options: SessionConnectionWithHistoryOptions): Promise<{ session: RealtimeSession; agent: RealtimeAgent }> {
    try {
      this.isDestroying = false;

      // Create session infrastructure
      const { agent, session } = this.createSession(options);

      // Authenticate and connect
      await this.authenticateAndConnect(session, options.onTokenRequest);

      // Use the pre-loaded history items (SSOT - same data UI received)
      const items = options.historyItems;
      if (items.length > 0) {
        session.updateHistory(items);
        logger.info(`📜 Hydrated ${items.length} history items into Realtime session (SSOT)`);
      } else {
        logger.debug('No conversation history to hydrate');
      }

      // Success feedback
      logger.info('✅ Session connected successfully');

      // Notify callback
      this.config.onSessionReady?.(session, agent);

      return { session, agent };

    } catch (error) {
      logger.error('❌ Failed to connect session:', error);
      this.config.onSessionError?.(error as Error);
      throw error;
    }
  }

  /**
   * @deprecated Use connectWithHistory() via bootstrapSession() for SSOT compliance.
   * This method is kept for backward compatibility but should not be used for new code.
   */
  async connect(options: SessionConnectionOptions): Promise<{ session: RealtimeSession; agent: RealtimeAgent }> {
    // Delegate to connectWithHistory with empty history
    return this.connectWithHistory({
      ...options,
      historyItems: []
    });
  }

  /**
   * Disconnect session
   */
  async disconnect(): Promise<void> {
    this.isDestroying = true;
    this.clearReconnectTimeout();

    try {
      // Disconnect the actual session (using any cast as disconnect might not be typed)
      if (this.currentSession) {
        logger.info('🔌 Disconnecting session...');
        const sessionAny = this.currentSession as any;
        if (sessionAny.disconnect) {
          await sessionAny.disconnect();
        }
        this.currentSession = undefined;
      }

      // Clear agent reference
      this.currentAgent = undefined;

      // Notify callback
      this.config.onSessionEnded?.();

      logger.info('✅ Session disconnected');
    } catch (error) {
      logger.error('❌ Error during disconnect:', error);
    }
  }

  /**
   * Get current session and agent
   */
  getCurrent(): { session?: RealtimeSession; agent?: RealtimeAgent } {
    return {
      session: this.currentSession,
      agent: this.currentAgent
    };
  }

  /**
   * Schedule reconnection
   */
  private scheduleReconnect(delay: number = 1000): void {
    if (this.isDestroying) return;

    this.clearReconnectTimeout();
    this.reconnectTimeout = setTimeout(() => {
      // Reconnection logic would go here
    }, delay);
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
