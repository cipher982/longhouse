/**
 * WebSocket Handler Module
 * Manages WebSocket message handling and event processing for realtime communication
 */

import { logger } from '@jarvis/core';
import type { RealtimeSession } from '@openai/agents/realtime';
import { stateManager } from './state-manager';
import { voiceManager } from './voice-manager';
import { uiController } from './ui-controller';
import { VoiceButtonState } from './config';
import type { ConversationRenderer } from './conversation-renderer';

/**
 * WebSocket message types
 */
export interface WebSocketMessage {
  type: string;
  data?: any;
  error?: string;
}

/**
 * WebSocket handler configuration
 */
export interface WebSocketHandlerConfig {
  onMessage?: (message: WebSocketMessage) => void;
  onTranscript?: (text: string, isFinal: boolean) => void;
  onAssistantMessage?: (text: string) => void;
  onError?: (error: Error) => void;
}

/**
 * WebSocket Handler class
 */
export class WebSocketHandler {
  private config: WebSocketHandlerConfig;
  private currentStreamingMessageId: string | null = null;

  constructor(config: WebSocketHandlerConfig = {}) {
    this.config = config;
  }

  /**
   * Setup session event handlers
   */
  setupSessionHandlers(session: RealtimeSession): void {
    // Transcript events
    session.on('transcript', (transcript) => {
      this.handleTranscript(transcript);
    });

    // Assistant message events
    session.on('message', (message) => {
      this.handleAssistantMessage(message);
    });

    // Turn start/end events
    session.on('turnStart', () => {
      this.handleTurnStart();
    });

    session.on('turnEnd', () => {
      this.handleTurnEnd();
    });

    // VAD events
    session.on('vadStart', () => {
      this.handleVADStart();
    });

    session.on('vadEnd', () => {
      this.handleVADEnd();
    });

    // Error events
    session.on('error', (error) => {
      this.handleError(error);
    });

    // Connection events
    session.on('connected', () => {
      this.handleConnected();
    });

    session.on('disconnected', () => {
      this.handleDisconnected();
    });
  }

  /**
   * Handle incoming transcript
   */
  private handleTranscript(transcript: any): void {
    const text = transcript.text || '';
    const isFinal = transcript.final || false;

    // Process through voice manager
    voiceManager.handleTranscript(text, isFinal);

    // Update UI
    const renderer = stateManager.getState().conversationRenderer;
    if (renderer) {
      if (!isFinal) {
        renderer.showPendingUserBubble(text);
      } else {
        renderer.finalizePendingUserBubble(text);
      }
    }

    // Notify callback
    this.config.onTranscript?.(text, isFinal);

    logger.debug('Transcript received:', { text, isFinal });
  }

  /**
   * Handle assistant message
   */
  private handleAssistantMessage(message: any): void {
    const text = message.text || '';

    // Update UI
    const renderer = stateManager.getState().conversationRenderer;
    if (renderer) {
      if (!this.currentStreamingMessageId) {
        this.currentStreamingMessageId = renderer.startStreamingMessage();
      }
      renderer.updateStreamingMessage(this.currentStreamingMessageId, text);
    }

    // Update state
    stateManager.setStreamingText(text);

    // Notify callback
    this.config.onAssistantMessage?.(text);

    logger.debug('Assistant message:', text);
  }

  /**
   * Handle turn start
   */
  private handleTurnStart(): void {
    // Update state to responding
    if (stateManager.isConnected()) {
      stateManager.setVoiceButtonState(VoiceButtonState.RESPONDING);
    }

    // Start new streaming message
    const renderer = stateManager.getState().conversationRenderer;
    if (renderer) {
      this.currentStreamingMessageId = renderer.startStreamingMessage();
    }

    logger.debug('Turn started');
  }

  /**
   * Handle turn end
   */
  private handleTurnEnd(): void {
    // Finalize streaming message
    if (this.currentStreamingMessageId) {
      const renderer = stateManager.getState().conversationRenderer;
      const text = stateManager.getState().currentStreamingText;

      if (renderer && text) {
        renderer.finalizeStreamingMessage(this.currentStreamingMessageId, text);
      }

      this.currentStreamingMessageId = null;
      stateManager.setStreamingText('');
    }

    // Update state back to ready
    if (stateManager.isResponding()) {
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
    }

    logger.debug('Turn ended');
  }

  /**
   * Handle VAD start
   */
  private handleVADStart(): void {
    voiceManager.handleVADStateChange(true);
    logger.debug('VAD started');
  }

  /**
   * Handle VAD end
   */
  private handleVADEnd(): void {
    voiceManager.handleVADStateChange(false);
    logger.debug('VAD ended');
  }

  /**
   * Handle error
   */
  private handleError(error: any): void {
    logger.error('WebSocket error:', error);

    // Show error in UI
    uiController.showError(error.message || 'Connection error');

    // Notify callback
    this.config.onError?.(error);
  }

  /**
   * Handle connected event
   */
  private handleConnected(): void {
    logger.info('WebSocket connected');
    uiController.updateStatus('Connected');
  }

  /**
   * Handle disconnected event
   */
  private handleDisconnected(): void {
    logger.info('WebSocket disconnected');

    // Clear any streaming message
    if (this.currentStreamingMessageId) {
      this.currentStreamingMessageId = null;
      stateManager.setStreamingText('');
    }

    uiController.updateStatus('Disconnected');
  }

  /**
   * Send message through WebSocket
   */
  sendMessage(message: WebSocketMessage): void {
    const session = stateManager.getState().session;
    if (!session) {
      logger.warn('Cannot send message - no session');
      return;
    }

    try {
      // Session would handle sending the message
      // This is a simplified version
      logger.debug('Sending message:', message);
    } catch (error) {
      logger.error('Failed to send message:', error);
      this.handleError(error);
    }
  }

  /**
   * Process incoming WebSocket message
   */
  processMessage(message: WebSocketMessage): void {
    // Route message based on type
    switch (message.type) {
      case 'transcript':
        this.handleTranscript(message.data);
        break;

      case 'assistant_message':
        this.handleAssistantMessage(message.data);
        break;

      case 'turn_start':
        this.handleTurnStart();
        break;

      case 'turn_end':
        this.handleTurnEnd();
        break;

      case 'vad_start':
        this.handleVADStart();
        break;

      case 'vad_end':
        this.handleVADEnd();
        break;

      case 'error':
        this.handleError(message.error);
        break;

      default:
        logger.debug('Unknown message type:', message.type);
    }

    // Notify callback
    this.config.onMessage?.(message);
  }

  /**
   * Get current streaming message ID
   */
  getCurrentStreamingMessageId(): string | null {
    return this.currentStreamingMessageId;
  }

  /**
   * Cleanup
   */
  cleanup(): void {
    this.currentStreamingMessageId = null;
  }
}

// Export singleton instance
export const websocketHandler = new WebSocketHandler();