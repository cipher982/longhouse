/**
 * WebSocket Handler Module
 * Manages realtime event processing
 */

import { logger } from '@jarvis/core';
import type { RealtimeSession } from '@openai/agents/realtime';

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
  private config: WebSocketHandlerConfig = {};

  /**
   * Set configuration
   */
  setConfig(config: WebSocketHandlerConfig): void {
    this.config = { ...this.config, ...config };
  }

  /**
   * Setup session event handlers (matches original main.ts pattern)
   */
  setupSessionHandlers(session: any): void {
    // Transport events (raw Realtime API events)
    session.on('transport_event', (event: any) => {
      this.handleTransportEvent(event);
    });

    // Error events
    session.on('error', (error: any) => {
      this.handleError(error);
    });

    // Connection events
    session.on('connected', () => {
      logger.info('WebSocket connected');
    });

    session.on('disconnected', () => {
      logger.info('WebSocket disconnected');
    });
  }

  /**
   * Handle transport events (replica of original implementation)
   */
  private handleTransportEvent(event: any): void {
    const eventType = event.type || '';
    logger.debug('Transport event:', eventType);

    // VAD speech start/stop
    if (eventType.includes('input_audio_buffer') && eventType.includes('speech_started')) {
      // Handle speech start
      return;
    }

    if (eventType.includes('input_audio_buffer') &&
        (eventType.includes('speech_stopped') || eventType.includes('speech_ended') || eventType.includes('speech_end'))) {
      // Handle speech end
      return;
    }

    // Partial transcription deltas
    if (eventType === 'conversation.item.input_audio_transcription.delta') {
      this.handleTranscript({ text: event.delta || '', final: false });
      return;
    }

    // Response output audio/text
    if (eventType.startsWith('response.output_audio') || eventType === 'response.output_text.delta') {
      this.handleAssistantMessage({ text: event.delta || '' });
      return;
    }

    // Response complete
    if (eventType === 'response.done') {
      // Handle response completion
      return;
    }

    // Final transcription
    if (eventType === 'conversation.item.input_audio_transcription.completed') {
      this.handleTranscript({ text: event.transcript || '', final: true });
      return;
    }
  }

  /**
   * Handle incoming transcript
   */
  private handleTranscript(transcript: any): void {
    const text = transcript.text || '';
    const isFinal = transcript.final || false;

    // Notify callback
    this.config.onTranscript?.(text, isFinal);

    logger.debug('Transcript received:', { text, isFinal });
  }

  /**
   * Handle assistant message
   */
  private handleAssistantMessage(message: any): void {
    const text = message.text || '';

    // Notify callback
    this.config.onAssistantMessage?.(text);

    logger.debug('Assistant message:', text);
  }

  /**
   * Handle error
   */
  private handleError(error: any): void {
    logger.error('WebSocket error:', error);

    // Notify callback
    this.config.onError?.(error);
  }

  /**
   * Send message through WebSocket
   */
  sendMessage(message: WebSocketMessage): void {
    try {
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
   * Cleanup
   */
  cleanup(): void {
    // Nothing to cleanup for now
  }
}

// Export singleton instance
export const websocketHandler = new WebSocketHandler();
