/**
 * ConversationRenderer - Single render architecture for message display
 * Eliminates race conditions by having only one DOM mutation point
 */

import type { Message, ConversationTurn } from '@jarvis/data-local';
import { userIcon, assistantIcon } from '../assets/icons';

export class ConversationRenderer {
  private messages: Message[] = [];
  private element: HTMLElement;
  private statusText: string | null = null;
  private statusMuted = false;
  private seqCounter = 0;

  constructor(element: HTMLElement) {
    this.element = element;
  }

  /**
   * Add a message and re-render the conversation
   */
  addMessage(message: Message): void {
    // Assign monotonic sequence if not present to break timestamp ties
    if (message.seq == null) message.seq = ++this.seqCounter;
    this.messages.push(message);
    this.renderConversation();
  }

  /**
   * Update an existing message (useful for streaming)
   */
  updateMessage(id: string, updates: Partial<Message>): void {
    const messageIndex = this.messages.findIndex(m => m.id === id);
    if (messageIndex !== -1) {
      const existing = this.messages[messageIndex];
      const merged = { ...existing, ...updates };
      // Preserve original timestamp and seq unless explicitly changed
      merged.timestamp = existing.timestamp;
      merged.seq = existing.seq;
      this.messages[messageIndex] = merged;
      this.renderConversation();
    }
  }

  /**
   * Load conversation history from ConversationTurn format
   */
  loadFromHistory(history: ConversationTurn[]): void {
    this.messages = [];

    for (const turn of history) {
      if (turn.userTranscript) {
        this.messages.push({
          id: `${turn.id}-user`,
          role: 'user',
          content: turn.userTranscript,
          timestamp: turn.timestamp,
          seq: ++this.seqCounter
        });
      }

      if (turn.assistantResponse) {
        this.messages.push({
          id: `${turn.id}-assistant`,
          role: 'assistant',
          content: turn.assistantResponse,
          timestamp: turn.timestamp,
          seq: ++this.seqCounter
        });
      }
    }

    this.renderConversation();
  }

  /**
   * Clear all messages
   */
  clear(): void {
    this.messages = [];
    this.renderConversation();
  }

  /** Set or clear a non-turn status banner (shown only when no messages). */
  setStatus(text: string, muted = false): void {
    this.statusText = text;
    this.statusMuted = muted;
    this.renderConversation();
  }

  clearStatus(): void {
    this.statusText = null;
    this.renderConversation();
  }

  /** Returns true if there is at least one user/assistant message. */
  hasTurns(): boolean {
    return this.messages.length > 0;
  }

  /** Optional helpers */
  getMessageCount(): number {
    return this.messages.length;
  }

  isStreamingActive(): boolean {
    return this.messages.some(m => !!m.isStreaming);
  }

  /**
   * Get all messages (for debugging)
   */
  getMessages(): Message[] {
    return [...this.messages];
  }

  /**
   * Single render function - handles all message display
   * This is the only place that mutates the DOM
   */
  private renderConversation(): void {
    if (this.messages.length === 0) {
      // Show status if available when there are no messages
      if (this.statusText) {
        this.element.innerHTML = `<div class="${this.statusMuted ? 'muted' : ''}">${this.escapeHtml(this.statusText)}</div>`;
      } else {
        this.element.innerHTML = '';
      }
    } else {
      // Clear status automatically when we have messages
      // Sort messages by timestamp, then by seq for deterministic order
      const sortedMessages = [...this.messages].sort((a, b) => {
        const dt = a.timestamp.getTime() - b.timestamp.getTime();
        if (dt !== 0) return dt;
        const as = a.seq ?? 0;
        const bs = b.seq ?? 0;
        return as - bs;
      });

      const html = sortedMessages.map(message => this.messageToHTML(message)).join('');
      this.element.innerHTML = html;
    }

    // Scroll to bottom
    this.element.scrollTop = this.element.scrollHeight;
  }

  /**
   * Convert message to HTML
   */
  private messageToHTML(message: Message): string {
    const timeStr = message.timestamp.toLocaleTimeString();
    const icon = message.role === 'user' ? userIcon : assistantIcon;
    const roleLabel = message.role === 'user' ? 'You' : 'Assistant';
    const streamingCursor = message.isStreaming ? '<span class="cursor">▋</span>' : '';
    const streamingClass = message.isStreaming ? ' streaming' : '';

    return `
      <div class="${message.role}-turn${streamingClass}">
        <div class="turn-header">${icon} ${roleLabel}</div>
        <div class="turn-content">${this.escapeHtml(message.content)}${streamingCursor}</div>
        <div class="turn-timestamp">${timeStr}</div>
      </div>
    `;
  }

  /**
   * Escape HTML to prevent XSS
   */
  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}
