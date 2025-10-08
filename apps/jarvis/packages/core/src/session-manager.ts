/**
 * Session Manager - Simple session-based data loading
 * Orchestrates IndexedDB → MemoryVectorStore → OpenAI session flow
 */

import { ConversationManager } from '@jarvis/data-local/conversation-manager';
import { MemoryVectorStore } from '@jarvis/data-local/memory-vector-store';
import type { ConversationTurn } from '@jarvis/data-local/types';
import type { ConversationManagerOptions } from '@jarvis/data-local/conversation-manager';
import { logger } from './logger';

export interface SessionContextConfig {
  name: string;
}

export interface SessionManagerDependencies {
  conversationManager?: ConversationManager;
  vectorStore?: MemoryVectorStore;
}

export interface SessionManagerOptions {
  conversationManagerOptions?: ConversationManagerOptions;
  maxHistoryTurns?: number;
}

export class SessionManager {
  private conversationManager: ConversationManager;
  private vectorStore: MemoryVectorStore;
  private currentContext: SessionContextConfig | null = null;
  private sessionActive = false;
  // Serialize writes for determinism and flush() barrier support
  private writeChain: Promise<void> = Promise.resolve();

  constructor(
    dependencies: SessionManagerDependencies = {},
    options: SessionManagerOptions = {}
  ) {
    const { conversationManagerOptions, maxHistoryTurns } = options;
    this.conversationManager = dependencies.conversationManager
      ?? new ConversationManager(maxHistoryTurns ?? 50, conversationManagerOptions);
    this.vectorStore = dependencies.vectorStore ?? new MemoryVectorStore(logger);
  }

  /**
   * Initialize a new session with context-specific data loading
   * Returns the current conversation ID if one exists
   */
  async initializeSession(config: SessionContextConfig, contextName: string): Promise<string | null> {
    logger.info(`Starting session for context: ${contextName}`);
    const startTime = performance.now();

    try {
      // 1. Initialize conversation manager with context-specific database
      await this.conversationManager.initialize(contextName);
      
      // 2. Auto-resume most recent conversation if exists
      const recentConversations = await this.conversationManager.getAllConversations();
      if (recentConversations.length > 0) {
        const mostRecent = recentConversations[0]; // Already sorted by updatedAt desc
        await this.conversationManager.switchToConversation(mostRecent.id);
        logger.success(`Auto-resumed conversation: ${mostRecent.name} (${mostRecent.id})`);
      }
      
      // 2. Load all documents from IndexedDB into memory for fast search
      const documents = await this.conversationManager.getAllDocuments();
      await this.vectorStore.initialize(documents);

      // 3. Load sample data if empty (for demo/development)
      if (documents.length === 0) {
        logger.context('Loading sample data for context', contextName);
        await this.conversationManager.loadSampleData();
        
        // Reinitialize vector store with sample data
        const sampleDocuments = await this.conversationManager.getAllDocuments();
        await this.vectorStore.initialize(sampleDocuments);
      }

      this.currentContext = config;
      this.sessionActive = true;

      const initTime = performance.now() - startTime;
      const stats = this.vectorStore.getStats();
      
      logger.performance('Session initialization', initTime, {
        documentCount: stats.documentCount,
        memoryUsageMB: stats.memoryUsageMB,
        contextName: config.name
      });
      logger.success(`Vector search ready for context: ${config.name}`);

      // Return current conversation ID for resumption
      const currentConversationId = await this.conversationManager.getCurrentConversationId();
      return currentConversationId;

    } catch (error) {
      logger.error('Session initialization failed', error);
      throw new Error(`Failed to initialize session for ${contextName}: ${error}`);
    }
  }

  configureSync(options: ConversationManagerOptions): void {
    this.conversationManager.configureSync(options);
  }

  /**
   * Get conversation manager for turn persistence
   */
  getConversationManager(): ConversationManager {
    if (!this.sessionActive) {
      throw new Error('Session not active - call initializeSession first');
    }
    return this.conversationManager;
  }

  /**
   * Get vector store for semantic search
   */
  getVectorStore(): MemoryVectorStore {
    if (!this.sessionActive) {
      throw new Error('Session not active - call initializeSession first');
    }
    return this.vectorStore;
  }

  /**
   * Get current context configuration
   */
  getCurrentContext(): SessionContextConfig | null {
    return this.currentContext;
  }

  /**
   * Add a conversation turn and persist it
   */
  async addConversationTurn(turn: ConversationTurn): Promise<void> {
    if (!this.sessionActive) {
      throw new Error('Session not active');
    }
    // Enqueue the write to ensure a single-writer model
    this.writeChain = this.writeChain.then(async () => {
      await this.conversationManager.addTurn(turn);
      // Also enqueue an outbox op for future sync
      try {
        await this.conversationManager.queueAppendTurnOp(turn);
      } catch (e) {
        // Non-fatal: outbox scaffolding may not be used yet
        logger.debug('Outbox enqueue skipped/failed', e);
      }
    });
    return this.writeChain;
  }

  /**
   * Get conversation history for context loading
   */
  async getConversationHistory(conversationId?: string): Promise<ConversationTurn[]> {
    if (!this.sessionActive) {
      throw new Error('Session not active');
    }
    
    return await this.conversationManager.getConversationHistory();
  }

  /**
   * Get recent conversation context for OpenAI session initialization
   */
  async getRecentConversationContext(limit: number = 8): Promise<ConversationTurn[]> {
    if (!this.sessionActive) {
      return [];
    }
    
    const history = await this.conversationManager.getConversationHistory();
    return history.slice(-limit); // Get last N turns for context
  }

  /**
   * Search documents semantically
   */
  async searchDocuments(query: string, options?: {
    limit?: number;
    type?: 'financial' | 'product' | 'policy' | 'organizational' | 'strategic';
    category?: string;
  }): Promise<any[]> {
    if (!this.sessionActive) {
      throw new Error('Session not active');
    }

    // For now, use text search (semantic search requires embeddings)
    return await this.vectorStore.textSearch(query, options);
  }

  /**
   * Create a new conversation
   */
  async createNewConversation(name?: string): Promise<string> {
    if (!this.sessionActive) {
      throw new Error('Session not active');
    }
    // Queue creation to preserve ordering with subsequent turns
    let id = '';
    await (this.writeChain = this.writeChain.then(async () => {
      id = await this.conversationManager.createNewConversation(name);
    }));
    return id;
  }

  /**
   * Switch to an existing conversation
   */
  async switchToConversation(conversationId: string): Promise<void> {
    if (!this.sessionActive) {
      throw new Error('Session not active');
    }
    
    await this.conversationManager.switchToConversation(conversationId);
  }

  /**
   * Clear all conversations and turns, preserving documents in the database.
   */
  async clearAllConversations(): Promise<void> {
    if (!this.sessionActive) {
      throw new Error('Session not active')
    }
    await this.conversationManager.clearAllConversations()
  }

  /**
   * Get all conversations for UI display
   */
  async getAllConversations(): Promise<Array<{id: string, name: string, createdAt: Date, updatedAt: Date}>> {
    if (!this.sessionActive) {
      throw new Error('Session not active');
    }
    
    return await this.conversationManager.getAllConversations();
  }

  /**
   * Get session statistics
   */
  getSessionStats(): {
    active: boolean;
    contextName: string | null;
    vectorStore: ReturnType<MemoryVectorStore['getStats']>;
  } {
    return {
      active: this.sessionActive,
      contextName: this.currentContext?.name || null,
      vectorStore: this.sessionActive ? this.vectorStore.getStats() : {
        documentCount: 0,
        types: {},
        memoryUsageMB: 0
      }
    };
  }

  /**
   * End session (cleanup resources)
   */
  async endSession(): Promise<void> {
    logger.info('Ending session');

    // Note: No need to save vector store - it's just a cache
    // ConversationManager already persists turns in real-time
    // Next session will rebuild vector store from IndexedDB

    await this.vectorStore.clear();
    this.currentContext = null;
    this.sessionActive = false;

    logger.success('Session ended, memory cleared');
  }

  /**
   * Flush barrier: resolves when all prior writes are durably committed
   */
  async flush(): Promise<void> {
    await this.writeChain;
  }

  /**
   * syncNow(): push outbox → pull ops. Server APIs may be stubs during Phase 2.
   */
  async syncNow(): Promise<{ pushed: number; pulled: number }> {
    if (!this.sessionActive) return { pushed: 0, pulled: 0 };

    // Ensure all local writes are committed before syncing
    await this.flush();

    // Push outbox ops (if any)
    let pushed = 0;
    try {
      pushed = await this.conversationManager.pushOutbox();
    } catch (e) {
      logger.warn('pushOutbox failed', e);
    }

    // Pull remote ops (apply later when multi-device is used)
    let pulled = 0;
    try {
      pulled = await this.conversationManager.pullAndApplyOps();
    } catch (e) {
      logger.warn('pull ops failed', e);
    }

    return { pushed, pulled };
  }

  /**
   * Check if session is active
   */
  isSessionActive(): boolean {
    return this.sessionActive;
  }
}
