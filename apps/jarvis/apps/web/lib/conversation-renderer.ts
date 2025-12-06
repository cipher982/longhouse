/**
 * ConversationRenderer - Single render architecture for message display
 * Eliminates race conditions by having only one DOM mutation point
 */

import type { Message, ConversationTurn } from '@jarvis/data-local';
import { userIcon, assistantIcon } from '../assets/icons';

export class ConversationRenderer {
  private messages: Message[] = [];
  private element: HTMLElement;
  private scrollContainer: HTMLElement;
  private statusText: string | null = null;
  private statusMuted = false;
  private seqCounter = 0;

  constructor(element: HTMLElement) {
    this.element = element;
    // Find the scrollable parent (.chat-container has overflow-y: auto)
    this.scrollContainer = element.closest('.chat-container') as HTMLElement || element;
  }

  /**
   * Check if user is near bottom (within threshold) - if so, auto-scroll is appropriate
   */
  private isNearBottom(threshold = 150): boolean {
    const { scrollTop, scrollHeight, clientHeight } = this.scrollContainer;
    return scrollHeight - scrollTop - clientHeight < threshold;
  }

  private scrollToBottom(): void {
    this.scrollContainer.scrollTop = this.scrollContainer.scrollHeight;
  }

  /**
   * Add a message and re-render the conversation
   * Always scrolls to bottom - new messages should be visible
   */
  addMessage(message: Message): void {
    // Assign monotonic sequence if not present to break timestamp ties
    if (message.seq == null) message.seq = ++this.seqCounter;
    this.messages.push(message);
    this.renderConversation(true); // Always scroll for new messages
  }

  /**
   * Update an existing message (useful for streaming)
   * Uses targeted DOM updates during streaming to prevent flashing
   */
  updateMessage(id: string, updates: Partial<Message>): void {
    // Check scroll position BEFORE DOM changes
    const shouldScroll = this.isNearBottom();

    const messageIndex = this.messages.findIndex(m => m.id === id);
    if (messageIndex !== -1) {
      const existing = this.messages[messageIndex];
      const merged = { ...existing, ...updates };
      // Preserve original timestamp and seq unless explicitly changed
      merged.timestamp = existing.timestamp;
      merged.seq = existing.seq;
      this.messages[messageIndex] = merged;

      // During streaming, do targeted DOM update to prevent flashing
      if (merged.isStreaming && updates.content !== undefined) {
        const updated = this.updateStreamingContent(id, merged.content, shouldScroll);
        if (updated) {
          return; // Skip full re-render
        }
      }

      this.renderConversation(shouldScroll);
    }
  }

  /**
   * Targeted update for streaming content - avoids full DOM replacement
   * Returns true if update was successful, false if full re-render needed
   */
  private updateStreamingContent(id: string, content: string, shouldScroll: boolean): boolean {
    const turnElement = this.element.querySelector(`[data-message-id="${id}"]`);
    if (!turnElement) return false;

    const contentElement = turnElement.querySelector('.turn-content');
    if (!contentElement) return false;

    // Update text content while preserving cursor
    const escapedContent = this.escapeHtml(content);
    contentElement.innerHTML = `${escapedContent}<span class="cursor">▋</span>`;

    if (shouldScroll) {
      this.scrollToBottom();
    }

    return true;
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
   * Uses incremental DOM updates to prevent flashing
   */
  private renderConversation(shouldScroll = true): void {
    if (this.messages.length === 0) {
      // Show status if available when there are no messages
      if (this.statusText) {
        this.element.innerHTML = `<div class="${this.statusMuted ? 'muted' : ''}">${this.escapeHtml(this.statusText)}</div>`;
      } else {
        this.element.innerHTML = '';
      }
      return;
    }

    // Sort messages by timestamp, then by seq for deterministic order
    const sortedMessages = [...this.messages].sort((a, b) => {
      const dt = a.timestamp.getTime() - b.timestamp.getTime();
      if (dt !== 0) return dt;
      const as = a.seq ?? 0;
      const bs = b.seq ?? 0;
      return as - bs;
    });

    // Build set of message IDs we need
    const neededIds = new Set(sortedMessages.map(m => m.id));

    // Remove DOM elements that are no longer needed
    this.element.querySelectorAll('[data-message-id]').forEach(el => {
      const id = el.getAttribute('data-message-id');
      if (id && !neededIds.has(id)) {
        el.remove();
      }
    });

    // Add or update messages
    let lastElement: Element | null = null;
    for (const message of sortedMessages) {
      const existingEl = this.element.querySelector(`[data-message-id="${message.id}"]`);

      if (existingEl) {
        // Update existing element if needed (e.g., streaming state changed)
        this.updateExistingElement(existingEl, message);
        lastElement = existingEl;
      } else {
        // Create and insert new element
        const newEl = this.createMessageElement(message);
        if (lastElement) {
          lastElement.after(newEl);
        } else {
          // Insert at beginning (or after any status element)
          const firstMessage = this.element.querySelector('[data-message-id]');
          if (firstMessage) {
            firstMessage.before(newEl);
          } else {
            this.element.appendChild(newEl);
          }
        }
        lastElement = newEl;
      }
    }

    if (shouldScroll) {
      this.scrollToBottom();
    }
  }

  /**
   * Update an existing DOM element with new message state
   */
  private updateExistingElement(el: Element, message: Message): void {
    // Update streaming class
    if (message.isStreaming) {
      el.classList.add('streaming');
    } else {
      el.classList.remove('streaming');
      // Update content when streaming ends
      const contentEl = el.querySelector('.turn-content');
      if (contentEl) {
        contentEl.innerHTML = this.escapeHtml(message.content);
      }
    }
  }

  /**
   * Create a new message DOM element
   */
  private createMessageElement(message: Message): HTMLElement {
    const template = document.createElement('template');
    template.innerHTML = this.messageToHTML(message).trim();
    return template.content.firstElementChild as HTMLElement;
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
      <div class="${message.role}-turn${streamingClass}" data-message-id="${message.id}">
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
