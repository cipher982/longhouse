/**
 * TextChannelController - Manages text message sending
 *
 * Responsibilities:
 * - Manage send queue with error handling and retries
 * - Ensure voice controller is muted before sending
 * - Auto-connect to session if needed
 * - Emit events for text activity
 *
 * Usage:
 *   const controller = new TextChannelController(session, voiceController);
 *   await controller.sendText("Hello, assistant!");
 */

import { eventBus } from './event-bus';
import type { RealtimeSession } from '@openai/agents/realtime';
import type { VoiceChannelController } from './voice-channel-controller';
import type { InteractionStateMachine } from './interaction-state-machine';

export interface TextMessage {
  id: string;
  text: string;
  timestamp: number;
  status: 'pending' | 'sending' | 'sent' | 'error';
  error?: Error;
  retryCount: number;
}

export interface TextChannelConfig {
  maxRetries?: number;
  retryDelay?: number;
  autoConnect?: boolean;
}

export class TextChannelController {
  private session: RealtimeSession | null = null;
  private voiceController: VoiceChannelController | null = null;
  private stateMachine: InteractionStateMachine | null = null;
  private connectCallback: (() => Promise<void>) | null = null;

  private config: TextChannelConfig;
  private queue: TextMessage[] = [];
  private sending: boolean = false;

  constructor(config: TextChannelConfig = {}) {
    this.config = {
      maxRetries: config.maxRetries || 3,
      retryDelay: config.retryDelay || 1000,
      autoConnect: config.autoConnect !== false  // Default to true
    };
  }

  /**
   * Initialize the controller
   */
  async initialize(): Promise<void> {
    console.log('[TextController] Initialized');
  }

  /**
   * Set the realtime session
   */
  setSession(session: RealtimeSession | null): void {
    this.session = session;
  }

  /**
   * Set the voice controller (to ensure muting before sending)
   */
  setVoiceController(voiceController: VoiceChannelController): void {
    this.voiceController = voiceController;
  }

  /**
   * Set the state machine (to switch to text mode)
   */
  setStateMachine(stateMachine: InteractionStateMachine): void {
    this.stateMachine = stateMachine;
  }

  /**
   * Set the connect callback (for auto-connect)
   */
  setConnectCallback(callback: () => Promise<void>): void {
    this.connectCallback = callback;
  }

  /**
   * Send a text message
   * @param text The message text to send
   * @returns Promise that resolves when the message is sent
   */
  async sendText(text: string): Promise<void> {
    if (!text || text.trim().length === 0) {
      throw new Error('Cannot send empty message');
    }

    const trimmedText = text.trim();

    // Create message object
    const message: TextMessage = {
      id: crypto.randomUUID(),
      text: trimmedText,
      timestamp: Date.now(),
      status: 'pending',
      retryCount: 0
    };

    // Add to queue
    this.queue.push(message);

    // Process queue
    await this.processQueue();
  }

  /**
   * Process the message queue
   */
  private async processQueue(): Promise<void> {
    if (this.sending) {
      // Already processing
      return;
    }

    if (this.queue.length === 0) {
      // Nothing to send
      return;
    }

    this.sending = true;

    try {
      while (this.queue.length > 0) {
        const message = this.queue[0];

        try {
          await this.sendMessage(message);

          // Success - remove from queue
          this.queue.shift();

          // Emit success event
          eventBus.emit('text_channel:sent', {
            text: message.text,
            timestamp: message.timestamp
          });
        } catch (error) {
          message.status = 'error';
          message.error = error instanceof Error ? error : new Error(String(error));
          message.retryCount++;

          // Check if we should retry
          if (message.retryCount < this.config.maxRetries!) {
            console.log(`[TextController] Retrying message (${message.retryCount}/${this.config.maxRetries})`);

            // Wait before retrying
            await this.sleep(this.config.retryDelay!);

            // Reset status for retry
            message.status = 'pending';
          } else {
            // Max retries exceeded - remove from queue and emit error
            console.error('[TextController] Max retries exceeded for message:', message.text);
            this.queue.shift();

            eventBus.emit('text_channel:error', {
              error: message.error,
              message: `Failed to send message after ${message.retryCount} retries: ${message.error.message}`
            });
          }
        }
      }
    } finally {
      this.sending = false;
    }
  }

  /**
   * Send a single message
   */
  private async sendMessage(message: TextMessage): Promise<void> {
    console.log('[TextController] Sending message:', message.text);

    // 1. Ensure we're in text mode
    if (this.stateMachine && this.stateMachine.isVoiceMode()) {
      console.log('[TextController] Switching to text mode');
      this.stateMachine.transitionToText();
    }

    // 2. Ensure voice is muted
    if (this.voiceController && this.voiceController.isArmed()) {
      console.log('[TextController] Muting voice channel');
      this.voiceController.mute();
    }

    // 3. Ensure session is connected
    if (!this.session) {
      if (this.config.autoConnect && this.connectCallback) {
        console.log('[TextController] Auto-connecting session...');
        await this.connectCallback();

        // Wait a bit for connection to stabilize
        await this.sleep(500);

        if (!this.session) {
          throw new Error('Failed to establish session');
        }
      } else {
        throw new Error('No active session and auto-connect is disabled');
      }
    }

    // 4. Send the message
    message.status = 'sending';
    eventBus.emit('text_channel:sending', { text: message.text });

    try {
      // sendMessage accepts a string or structured message
      this.session.sendMessage(message.text);
      message.status = 'sent';
      console.log('[TextController] Message sent successfully');
    } catch (error) {
      console.error('[TextController] Failed to send message:', error);
      throw error;
    }
  }

  /**
   * Get the current queue status
   */
  getQueueStatus(): {
    pending: number;
    sending: boolean;
    messages: Array<{ text: string; status: string; retryCount: number }>;
  } {
    return {
      pending: this.queue.length,
      sending: this.sending,
      messages: this.queue.map(m => ({
        text: m.text,
        status: m.status,
        retryCount: m.retryCount
      }))
    };
  }

  /**
   * Clear the message queue
   */
  clearQueue(): void {
    console.log('[TextController] Clearing message queue');
    this.queue = [];
  }

  /**
   * Sleep utility
   */
  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  /**
   * Clean up resources
   */
  dispose(): void {
    this.clearQueue();
    this.session = null;
    this.voiceController = null;
    this.stateMachine = null;
    this.connectCallback = null;
    console.log('[TextController] Disposed');
  }
}
