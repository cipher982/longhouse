/**
 * Conversation UI Manager - ChatGPT-style sidebar
 */

import { SessionManager } from '@jarvis/core';

export class ConversationUI {
  private sessionManager: SessionManager | null = null;
  private conversationListEl: HTMLElement;
  private currentConversationId: string | null = null;
  private updateInterval: number | null = null;

  constructor() {
    this.conversationListEl = document.getElementById('conversationList')!;
    
    // Update timestamps every 30 seconds
    this.updateInterval = window.setInterval(() => {
      this.updateTimestamps();
    }, 30000);
  }

  setSessionManager(sessionManager: SessionManager): void {
    this.sessionManager = sessionManager;
  }

  /**
   * Load and display all conversations
   */
  async loadConversations(): Promise<void> {
    if (!this.sessionManager) {
      console.warn('ConversationUI: SessionManager not set');
      return;
    }

    try {
      const conversations = await this.sessionManager.getAllConversations();
      this.renderConversations(conversations);
    } catch (error) {
      console.error('Failed to load conversations:', error);
      this.renderEmptyState();
    }
  }

  /**
   * Render conversations in sidebar
   */
  private renderConversations(conversations: Array<{id: string, name: string, createdAt: Date, updatedAt: Date}>): void {
    if (conversations.length === 0) {
      this.renderEmptyState();
      return;
    }

    this.conversationListEl.innerHTML = conversations.map((conv, index) => {
      const displayName = this.generateBetterName(conv, index);
      const timeAgo = this.formatRelativeTime(conv.updatedAt);
      
      return `
        <div class="conversation-item" data-conversation-id="${conv.id}">
          <div class="conversation-name">${this.escapeHtml(displayName)}</div>
          <div class="conversation-meta">${timeAgo}</div>
        </div>
      `;
    }).join('');

    // Add click handlers
    this.conversationListEl.querySelectorAll('.conversation-item').forEach(item => {
      item.addEventListener('click', (e) => {
        const conversationId = (e.currentTarget as HTMLElement).dataset.conversationId;
        if (conversationId) {
          this.switchToConversation(conversationId);
        }
      });
    });
  }

  /**
   * Render empty state
   */
  private renderEmptyState(): void {
    this.conversationListEl.innerHTML = `
      <div class="conversation-item empty">
        <div class="conversation-name">No conversations yet</div>
        <div class="conversation-meta">Start your first conversation</div>
      </div>
    `;
  }

  /**
   * Switch to a conversation
   */
  private async switchToConversation(conversationId: string): Promise<void> {
    if (!this.sessionManager) return;

    try {
      // Update UI to show loading
      this.setActiveConversation(conversationId);
      
      // Switch conversation in session manager
      await this.sessionManager.switchToConversation(conversationId);
      this.currentConversationId = conversationId;
      
      // Dispatch event for main app to handle
      window.dispatchEvent(new CustomEvent('conversationSwitched', {
        detail: { conversationId }
      }));

      console.log(`Switched to conversation: ${conversationId}`);
    } catch (error) {
      console.error('Failed to switch conversation:', error);
    }
  }

  /**
   * Set active conversation in UI
   */
  setActiveConversation(conversationId: string): void {
    this.currentConversationId = conversationId;
    
    // Remove active class from all items
    this.conversationListEl.querySelectorAll('.conversation-item').forEach(item => {
      item.classList.remove('active');
    });

    // Add active class to selected item
    const activeItem = this.conversationListEl.querySelector(`[data-conversation-id="${conversationId}"]`);
    if (activeItem) {
      activeItem.classList.add('active');
    }
  }

  /**
   * Add new conversation to UI
   */
  async addNewConversation(conversationId: string, name?: string): Promise<void> {
    // Refresh the conversation list
    await this.loadConversations();
    
    // Set as active
    this.setActiveConversation(conversationId);
  }

  /**
   * Update conversation name (when first message is sent)
   */
  async updateConversationName(conversationId: string, name: string): Promise<void> {
    const conversationItem = this.conversationListEl.querySelector(`[data-conversation-id="${conversationId}"]`);
    if (conversationItem) {
      const nameEl = conversationItem.querySelector('.conversation-name');
      if (nameEl) {
        nameEl.textContent = name;
      }
    }
  }

  /**
   * Generate better conversation names
   */
  private generateBetterName(conversation: {name: string, createdAt: Date, updatedAt: Date}, index: number): string {
    const timeAgo = this.formatRelativeTime(conversation.updatedAt);
    
    // If it's an auto-generated timestamp name (contains colons and numbers)
    if (conversation.name.includes(':') && /\d+/.test(conversation.name)) {
      // Use time-based naming for clarity
      if (index === 0) return `Latest conversation`;
      return `Chat from ${timeAgo}`;
    }
    
    // If it's a proper name, keep it but show time more prominently  
    return conversation.name;
  }

  /**
   * Format relative time with more precision
   */
  private formatRelativeTime(date: Date): string {
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const seconds = Math.floor(diff / 1000);
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);

    if (seconds < 30) return 'Just now';
    if (seconds < 60) return `${seconds}s ago`;
    if (minutes < 60) return `${minutes}m ago`;
    if (hours < 24) return `${hours}h ago`;
    if (days < 7) return `${days}d ago`;
    
    return date.toLocaleDateString();
  }

  /**
   * Escape HTML to prevent XSS
   */
  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Get current conversation ID
   */
  getCurrentConversationId(): string | null {
    return this.currentConversationId;
  }

  /**
   * Clear conversations (for context switching)
   */
  clearConversations(): void {
    this.renderEmptyState();
    this.currentConversationId = null;
  }

  /**
   * Update timestamps without full reload
   */
  private async updateTimestamps(): Promise<void> {
    const metaElements = this.conversationListEl.querySelectorAll('.conversation-meta');
    if (!this.sessionManager || metaElements.length === 0) return;

    try {
      const conversations = await this.sessionManager.getAllConversations();
      metaElements.forEach((metaEl, index) => {
        if (conversations[index]) {
          metaEl.textContent = this.formatRelativeTime(conversations[index].updatedAt);
        }
      });
    } catch (error) {
      // Silently fail timestamp updates
      console.debug('Failed to update timestamps:', error);
    }
  }

  /**
   * Cleanup interval on destroy
   */
  destroy(): void {
    if (this.updateInterval) {
      clearInterval(this.updateInterval);
      this.updateInterval = null;
    }
  }
}
