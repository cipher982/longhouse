/**
 * Conversation Controller
 * Owns all conversation/turn management, streaming, and persistence
 *
 * Responsibilities:
 * - Manage conversation turns (user/assistant)
 * - Handle streaming responses
 * - Persist to IndexedDB via SessionManager
 * - Render to UI via ConversationRenderer
 */

import { logger, type SessionManager } from '@jarvis/core';
import type { ConversationTurn } from '@jarvis/data-local';
import type { ConversationRenderer } from './conversation-renderer';

export interface ConversationState {
  conversationId: string | null;
  streamingMessageId: string | null;
  streamingText: string;
  pendingUserMessageId: string | null;
}

export type ConversationEvent =
  | { type: 'streamingStart' }
  | { type: 'streamingStop' }
  | { type: 'conversationIdChange', id: string | null };

type ConversationListener = (event: ConversationEvent) => void;

export class ConversationController {
  private state: ConversationState = {
    conversationId: null,
    streamingMessageId: null,
    streamingText: '',
    pendingUserMessageId: null
  };

  private sessionManager: SessionManager | null = null;
  private renderer: ConversationRenderer | null = null;
  private listeners: Set<ConversationListener> = new Set();

  constructor() {}

  addListener(listener: ConversationListener): void {
    this.listeners.add(listener);
  }

  removeListener(listener: ConversationListener): void {
    this.listeners.delete(listener);
  }

  private emit(event: ConversationEvent): void {
    this.listeners.forEach(l => l(event));
  }

  // ============= Setup =============

  /**
   * Set the session manager for persistence
   */
  setSessionManager(sessionManager: SessionManager | null): void {
    this.sessionManager = sessionManager;
  }

  /**
   * Set the conversation renderer for UI updates
   */
  setRenderer(renderer: ConversationRenderer | null): void {
    this.renderer = renderer;
  }

  /**
   * Set current conversation ID
   */
  setConversationId(id: string | null): void {
    this.state.conversationId = id;
    this.emit({ type: 'conversationIdChange', id });
  }

  /**
   * Get current conversation ID
   */
  getConversationId(): string | null {
    return this.state.conversationId;
  }

  // ============= Turn Management =============

  /**
   * Update a pending user turn (preview)
   */
  async updateUserPreview(transcript: string): Promise<void> {
    if (!this.renderer) return;

    if (!this.state.pendingUserMessageId) {
      // Create new pending message
      this.state.pendingUserMessageId = `user-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      this.renderer.addMessage({
        id: this.state.pendingUserMessageId,
        role: 'user',
        content: transcript,
        timestamp: new Date()
      });
    } else {
      // Update existing
      this.renderer.updateMessage(this.state.pendingUserMessageId, {
        content: transcript
      });
    }
  }

  /**
   * Add user turn to UI and persist
   */
  async addUserTurn(transcript: string, timestamp?: Date): Promise<void> {
    if (!this.renderer) return;

    // If we have a pending message, use it to preserve the original timestamp
    if (this.state.pendingUserMessageId && !timestamp) {
      this.renderer.updateMessage(this.state.pendingUserMessageId, {
        content: transcript
      });
      this.state.pendingUserMessageId = null;
    } else {
      // Create new if no pending or if loading from history
      const messageTimestamp = timestamp || new Date();
      const messageId = `user-${Date.now()}-${Math.random().toString(36).slice(2)}`;

      this.renderer.addMessage({
        id: messageId,
        role: 'user',
        content: transcript,
        timestamp: messageTimestamp
      });
    }

    // Record to IndexedDB if not from history loading (no timestamp provided)
    if (!timestamp) {
      await this.recordTurn('user', transcript);
    }
  }

  /**
   * Add assistant turn to UI and persist
   */
  async addAssistantTurn(response: string, timestamp?: Date): Promise<void> {
    if (!this.renderer) return;

    const messageTimestamp = timestamp || new Date();
    const messageId = `assistant-${Date.now()}-${Math.random().toString(36).slice(2)}`;

    this.renderer.addMessage({
      id: messageId,
      role: 'assistant',
      content: response,
      timestamp: messageTimestamp
    });

    // Record to IndexedDB if not from history loading
    if (!timestamp) {
      await this.recordTurn('assistant', response);
    }
  }

  /**
   * Record turn to IndexedDB
   */
  private async recordTurn(type: 'user' | 'assistant', content: string): Promise<void> {
    if (!this.sessionManager) return;

    try {
      const turn: ConversationTurn = {
        id: crypto.randomUUID(),
        timestamp: new Date(),
        conversationId: this.state.conversationId || undefined,
        ...(type === 'user'
          ? { userTranscript: content }
          : { assistantResponse: content }
        )
      };

      await this.sessionManager.addConversationTurn(turn);
      logger.debug('Recorded conversation turn', `${type}: ${content.substring(0, 50)}...`);
    } catch (error) {
      console.error('Failed to record conversation turn:', error);
    }
  }

  // ============= Streaming Response Management =============

  /**
   * Start a streaming response
   */
  startStreaming(): void {
    logger.debug('Starting streaming response');

    if (!this.renderer) return;

    // Create streaming message
    this.state.streamingMessageId = `streaming-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    this.state.streamingText = '';

    this.renderer.addMessage({
      id: this.state.streamingMessageId,
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true
    });

    this.emit({ type: 'streamingStart' });
  }

  /**
   * Append text to streaming response
   */
  appendStreaming(delta: string): void {
    if (!this.state.streamingMessageId) {
      // Start streaming if not already started
      this.startStreaming();
    }

    this.state.streamingText += delta;

    if (this.renderer && this.state.streamingMessageId) {
      this.renderer.updateMessage(this.state.streamingMessageId, {
        content: this.state.streamingText,
        isStreaming: true
      });
    }
  }

  /**
   * Finalize streaming response
   */
  async finalizeStreaming(): Promise<void> {
    if (!this.renderer || !this.state.streamingMessageId) return;

    logger.streamingResponse(this.state.streamingText, true);

    // Finalize message in renderer
    this.renderer.updateMessage(this.state.streamingMessageId, {
      content: this.state.streamingText,
      isStreaming: false
    });

    // Record to IndexedDB
    if (this.state.streamingText) {
      await this.recordTurn('assistant', this.state.streamingText);
    }

    // Clean up streaming state
    this.state.streamingMessageId = null;
    this.state.streamingText = '';

    this.emit({ type: 'streamingStop' });
  }

  /**
   * Get current streaming text
   */
  getStreamingText(): string {
    return this.state.streamingText;
  }

  /**
   * Check if currently streaming
   */
  isStreaming(): boolean {
    return this.state.streamingMessageId !== null;
  }

  // ============= History Management =============

  /**
   * Load conversation history from IndexedDB and display in UI
   */
  async loadHistory(): Promise<void> {
    if (!this.sessionManager || !this.state.conversationId || !this.renderer) {
      if (this.renderer) {
        this.renderer.clear();
        this.renderer.setStatus('Tap the microphone to start', true);
      }
      return;
    }

    try {
      const history = await this.sessionManager.getConversationHistory();

      if (history.length === 0) {
        this.renderer.clear();
        this.renderer.setStatus('No messages yet - tap the microphone to start', true);
        return;
      }

      // Use renderer's loadFromHistory method
      this.renderer.loadFromHistory(history);

      logger.conversation('Loaded conversation turns into UI', history.length);
    } catch (error) {
      console.error('Failed to load conversation history:', error);
      if (this.renderer) {
        this.renderer.clear();
        this.renderer.setStatus('Failed to load conversation history', true);
      }
    }
  }

  /**
   * Clear conversation display
   */
  clear(): void {
    this.renderer?.clear();
    this.state.streamingMessageId = null;
    this.state.streamingText = '';
    this.state.pendingUserMessageId = null;
  }

  /**
   * Set status message in conversation area
   */
  setStatus(message: string, placeholder: boolean = false): void {
    this.renderer?.setStatus(message, placeholder);
  }

  // ============= Conversation Item Events (OpenAI Realtime) =============

  /**
   * Handle conversation.item.added event from OpenAI
   */
  handleItemAdded(event: any): void {
    logger.debug('Conversation item added', event);
  }

  /**
   * Handle conversation.item.done event from OpenAI
   */
  handleItemDone(event: any): void {
    logger.debug('Conversation item done', event);
  }

  // ============= Cleanup =============

  /**
   * Clean up resources
   */
  dispose(): void {
    this.clear();
    this.sessionManager = null;
    this.renderer = null;
    logger.info('Conversation controller disposed');
  }
}

// Export singleton instance
export const conversationController = new ConversationController();
