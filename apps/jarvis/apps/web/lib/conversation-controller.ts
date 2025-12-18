/**
 * Conversation Controller
 * Owns all conversation/turn management, streaming, and persistence
 *
 * Responsibilities:
 * - Manage conversation turns (user/assistant)
 * - Handle streaming responses
 * - Emit state changes via stateManager (React handles UI)
 *
 * NOTE: This controller does NOT manipulate the DOM directly.
 * All UI updates are done via stateManager events → React hooks → React state.
 */

import { logger } from '@jarvis/core';
import { stateManager } from './state-manager';

export interface ConversationState {
  conversationId: string | null;
  streamingMessageId: string | null;
  streamingText: string;
  pendingUserMessageId: string | null;
  currentCorrelationId: string | null;
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
    pendingUserMessageId: null,
    currentCorrelationId: null,
  };

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
   * NOTE: This is now a no-op since React handles user input display
   */
  async updateUserPreview(_transcript: string): Promise<void> {
    // React handles user input preview via TextInput component
    // This method kept for backward compatibility but does nothing
  }

  /**
   * Add user turn and persist to IndexedDB
   *
   * DEPRECATED: Jarvis web now uses server-side persistence (Supervisor/Postgres).
   * The React UI is responsible for optimistic rendering of user messages.
   *
   * Kept as a no-op for backwards compatibility with older call sites/tests.
   */
  async addUserTurn(_transcript: string, _timestamp?: Date): Promise<boolean> {
    this.state.pendingUserMessageId = null;
    return true;
  }

  /**
   * Add assistant turn and persist to IndexedDB
   *
   * DEPRECATED: Jarvis web now uses server-side persistence (Supervisor/Postgres).
   * Kept as a no-op for backwards compatibility.
   */
  async addAssistantTurn(_response: string, _timestamp?: Date): Promise<void> {
    return;
  }

  // ============= Streaming Response Management =============

  /**
   * Start a streaming response
   */
  startStreaming(correlationId?: string): void {
    logger.debug(`Starting streaming response, correlationId: ${correlationId}`);

    // Create streaming message ID
    this.state.streamingMessageId = `streaming-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    this.state.streamingText = '';
    this.state.currentCorrelationId = correlationId || null;

    this.emit({ type: 'streamingStart' });
  }

  /**
   * Append text to streaming response
   */
  appendStreaming(delta: string, correlationId?: string): void {
    if (!this.state.streamingMessageId) {
      // Start streaming if not already started
      this.startStreaming(correlationId);
    }

    this.state.streamingText += delta;

    // Notify React via stateManager
    stateManager.setStreamingText(this.state.streamingText);

    // If we have a correlationId, update that specific bubble too
    const activeCorrelationId = correlationId || this.state.currentCorrelationId;
    if (activeCorrelationId) {
      stateManager.updateAssistantStatus(activeCorrelationId, 'streaming', this.state.streamingText);
    }
  }

  /**
   * Finalize streaming response
   */
  async finalizeStreaming(): Promise<void> {
    if (!this.state.streamingMessageId) return;

    const finalText = this.state.streamingText;
    const correlationId = this.state.currentCorrelationId;
    logger.streamingResponse(finalText, true);

    // Clean up streaming state
    this.state.streamingMessageId = null;
    this.state.streamingText = '';
    this.state.currentCorrelationId = null;

    // Clear streaming text and notify React of finalized message
    stateManager.setStreamingText('');
    if (correlationId) {
      stateManager.finalizeMessage(finalText, correlationId);
    } else {
      stateManager.finalizeMessage(finalText);
    }

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

  /**
   * Clear controller state
   */
  clear(): void {
    this.state.streamingMessageId = null;
    this.state.streamingText = '';
    this.state.pendingUserMessageId = null;
    stateManager.setStreamingText('');
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
   * This event contains the complete item with all content
   */
  handleItemDone(event: any): void {
    logger.debug('Conversation item done', event);

    // Extract assistant response from item.done event
    // Structure: event.item.content[].text or event.item.content[].transcript
    const item = event?.item;
    if (!item || item.role !== 'assistant') {
      return;
    }

    // Get text from content array
    const content = item.content;
    if (!Array.isArray(content)) {
      return;
    }

    // Find text content (could be type 'text' or 'audio' with transcript)
    for (const part of content) {
      const text = part.text || part.transcript;
      if (text && typeof text === 'string' && text.trim()) {
        // If we're not already streaming this content, emit it
        // This handles the case where text.delta events weren't received
        if (!this.isStreaming() || this.state.streamingText !== text) {
          // Start fresh streaming with the complete text
          this.startStreaming();
          this.state.streamingText = text;
          stateManager.setStreamingText(text);
        }
        break;
      }
    }
  }

  // ============= Cleanup =============

  /**
   * Clean up resources
   */
  dispose(): void {
    this.clear();
    logger.info('Conversation controller disposed');
  }
}

// Export singleton instance
export const conversationController = new ConversationController();
