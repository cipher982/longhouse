/**
 * Conversation Controller
 * Owns all conversation/turn management, streaming, and persistence
 *
 * Responsibilities:
 * - Manage conversation turns (user/assistant)
 * - Handle streaming responses
 * - Persist to IndexedDB via SessionManager
 * - Emit state changes via stateManager (React handles UI)
 *
 * NOTE: This controller does NOT manipulate the DOM directly.
 * All UI updates are done via stateManager events → React hooks → React state.
 */

import { logger, type SessionManager } from '@jarvis/core';
import type { ConversationTurn } from '@jarvis/data-local';
import { stateManager } from './state-manager';
import { toSidebarConversations } from './conversation-list';
import { CONFIG } from './config';
import { isTestMode } from './test-helpers';

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
  private listeners: Set<ConversationListener> = new Set();
  private autoTitleInFlight: Set<string> = new Set();

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
   * NOTE: React UI handles displaying the message, this only persists
   * @returns true if persisted successfully, false if skipped (no sessionManager or timestamp provided)
   */
  async addUserTurn(transcript: string, timestamp?: Date): Promise<boolean> {
    // Clear any pending message state
    this.state.pendingUserMessageId = null;

    // Record to IndexedDB if not from history loading (no timestamp provided)
    if (!timestamp) {
      return await this.recordTurn('user', transcript);
    }
    return false; // Skipped persistence (from history loading)
  }

  /**
   * Add assistant turn and persist to IndexedDB
   * NOTE: React UI handles displaying the message, this only persists
   */
  async addAssistantTurn(response: string, timestamp?: Date): Promise<void> {
    // Record to IndexedDB if not from history loading
    if (!timestamp) {
      await this.recordTurn('assistant', response);
    }
  }

  /**
   * Record turn to IndexedDB
   * @returns true if persisted successfully, false if no sessionManager
   */
  private async recordTurn(type: 'user' | 'assistant', content: string): Promise<boolean> {
    if (!this.sessionManager) {
      logger.warn('Cannot persist turn: sessionManager not initialized');
      return false;
    }

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

      // ConversationManager may auto-create a conversation on first turn.
      // Sync the resulting conversation ID + sidebar list back to the UI.
      try {
        const activeId = await this.sessionManager.getConversationManager().getCurrentConversationId();
        if (activeId && activeId !== this.state.conversationId) {
          this.setConversationId(activeId);
          stateManager.setConversationId(activeId);
        }

        const allConversations = await this.sessionManager.getAllConversations();
        stateManager.setConversations(toSidebarConversations(allConversations, activeId ?? this.state.conversationId));

        if (type === 'assistant') {
          void this.maybeAutoTitleConversation(activeId ?? this.state.conversationId).catch((e) => {
            logger.debug('Auto-title skipped/failed', e);
          });
        }
      } catch (e) {
        logger.debug('Conversation list refresh skipped/failed', e);
      }

      return true;
    } catch (error) {
      console.error('Failed to record conversation turn:', error);
      return false;
    }
  }

  private buildJsonHeaders(): HeadersInit {
    // Cookie-based auth - no Authorization header needed
    // Cookies are sent automatically with credentials: 'include' on fetch calls
    return { 'Content-Type': 'application/json' };
  }

  private isDefaultConversationName(name: string): boolean {
    return name.trim().startsWith('Conversation ');
  }

  private async maybeAutoTitleConversation(conversationId: string | null | undefined): Promise<void> {
    if (!this.sessionManager) return;
    if (!conversationId) return;
    if (typeof window !== 'undefined' && isTestMode()) return;
    if (this.autoTitleInFlight.has(conversationId)) return;

    const conversationManager = this.sessionManager.getConversationManager();
    const titledKey = `conversation_title_generated:${conversationId}`;
    this.autoTitleInFlight.add(conversationId);
    try {
      const alreadyTitled = await conversationManager.getKV<boolean>(titledKey);
      if (alreadyTitled) return;

      const conversation = await conversationManager.getConversation(conversationId);
      if (!conversation) return;
      if (!this.isDefaultConversationName(conversation.name)) return;

      const recent = await this.sessionManager.getRecentConversationContext(8);
      const messages: Array<{ role: 'user' | 'assistant'; content: string }> = [];
      for (const t of recent) {
        const user = typeof t.userTranscript === 'string' ? t.userTranscript.trim() : '';
        const assistant = typeof (t.assistantResponse || t.assistantText) === 'string'
          ? String(t.assistantResponse || t.assistantText).trim()
          : '';
        if (user) messages.push({ role: 'user', content: user });
        if (assistant) messages.push({ role: 'assistant', content: assistant });
      }

      const hasUser = messages.some(m => m.role === 'user');
      const hasAssistant = messages.some(m => m.role === 'assistant');
      if (!hasUser || !hasAssistant) return;

      // Call Jarvis BFF → jarvis-server to generate a short title.
      const resp = await fetch(`${CONFIG.JARVIS_API_BASE}/conversation/title`, {
        method: 'POST',
        headers: this.buildJsonHeaders(),
        credentials: 'include', // Cookie auth
        body: JSON.stringify({ messages }),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      const title = typeof data?.title === 'string' ? data.title.trim() : '';
      if (!title) return;

      await this.sessionManager.renameConversation(conversationId, title);
      await conversationManager.setKV(titledKey, true);

      const allConversations = await this.sessionManager.getAllConversations();
      stateManager.setConversations(toSidebarConversations(allConversations, conversationId));
    } finally {
      this.autoTitleInFlight.delete(conversationId);
    }
  }

  // ============= Streaming Response Management =============

  /**
   * Start a streaming response
   */
  startStreaming(): void {
    logger.debug('Starting streaming response');

    // Create streaming message ID
    this.state.streamingMessageId = `streaming-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    this.state.streamingText = '';

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

    // Notify React via stateManager
    stateManager.setStreamingText(this.state.streamingText);
  }

  /**
   * Finalize streaming response
   */
  async finalizeStreaming(): Promise<void> {
    if (!this.state.streamingMessageId) return;

    const finalText = this.state.streamingText;
    logger.streamingResponse(finalText, true);

    // Record to IndexedDB
    if (finalText) {
      await this.recordTurn('assistant', finalText);
    }

    // Clean up streaming state
    this.state.streamingMessageId = null;
    this.state.streamingText = '';

    // Clear streaming text and notify React of finalized message
    stateManager.setStreamingText('');
    stateManager.finalizeMessage(finalText);

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
   * Get conversation history from IndexedDB
   * Returns history array for React to render
   */
  async getHistory(): Promise<ConversationTurn[]> {
    if (!this.sessionManager || !this.state.conversationId) {
      return [];
    }

    try {
      const history = await this.sessionManager.getConversationHistory();
      logger.conversation('Retrieved conversation history', history.length);
      return history;
    } catch (error) {
      console.error('Failed to load conversation history:', error);
      return [];
    }
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
    this.sessionManager = null;
    logger.info('Conversation controller disposed');
  }
}

// Export singleton instance
export const conversationController = new ConversationController();
