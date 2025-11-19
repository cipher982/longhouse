/**
 * Session Handler Module
 * Manages OpenAI Realtime session lifecycle
 */

import { RealtimeAgent, RealtimeSession, OpenAIRealtimeWebRTC } from '@openai/agents/realtime';
import { logger } from '@jarvis/core';
import { VoiceButtonState, CONFIG } from './config';
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
 * Session handler configuration
 */
export interface SessionHandlerConfig {
  onSessionReady?: (session: RealtimeSession, agent: RealtimeAgent) => void;
  onSessionError?: (error: Error) => void;
  onSessionEnded?: () => void;
  onSessionEvent?: (event: string, data: any) => void;
}

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
   * Create and connect a session with real OpenAI implementation
   */
  async connect(options: SessionConnectionOptions): Promise<{ session: RealtimeSession; agent: RealtimeAgent }> {
    try {
      this.isDestroying = false;

      // Always create a new agent to ensure tools are fresh
      // This ensures context/tool changes are picked up on reconnect
      const agent = new RealtimeAgent({
        name: options.context.name,
        instructions: options.context.instructions,
        tools: options.tools || []
      });

      logger.info(`🤖 Created agent: ${options.context.name} with ${options.tools?.length || 0} tools`);

      // Create WebRTC transport
      const transport = new OpenAIRealtimeWebRTC({
        mediaStream: options.mediaStream,
        audioElement: options.audioElement,
      });

      // Create the RealtimeSession
      const session = new RealtimeSession(agent, {
        transport,
        model: 'gpt-realtime',
        config: {
          inputAudioTranscription: { model: 'whisper-1' },
          audio: {
            output: {
              voice: 'verse',
              speed: 1.3  // 30% faster speech
            }
          },
          turnDetection: {
            type: 'server_vad',
            threshold: 0.5,
            prefix_padding_ms: 300,
            silence_duration_ms: 1500
          }
        }
      });

      // Store references
      this.currentSession = session;
      this.currentAgent = agent;

      // Get token and connect
      if (options.onTokenRequest) {
        logger.info('🎫 Requesting session token...');
        const token = await options.onTokenRequest();
        logger.info('🔌 Connecting to OpenAI Realtime...');
        await session.connect({ apiKey: token });
      } else {
        throw new Error('No token request handler provided');
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
