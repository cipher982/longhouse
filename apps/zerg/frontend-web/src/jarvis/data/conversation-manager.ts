/**
 * Conversation management with IndexedDB persistence
 * Handles multi-turn conversation memory and storage
 */

import { openDB, type DBSchema, type IDBPDatabase } from 'idb'
import type { ConversationTurn, CompanyDocument, OutboxOp, TurnRole } from './types'

interface SyncResponse {
  ok: boolean
  status: number
  json(): Promise<unknown>
}

interface SyncRequestInit {
  method?: string
  headers?: Record<string, string>
  body?: string
}

export type SyncTransport = (input: string, init?: SyncRequestInit) => Promise<SyncResponse>

export interface ConversationManagerOptions {
  syncTransport?: SyncTransport
  syncBaseUrl?: string
}

interface ConversationDB extends DBSchema {
  conversations: {
    key: string
    value: {
      id: string
      name: string
      createdAt: Date
      updatedAt: Date
      turns: ConversationTurn[]
    }
  }
  turns: {
    key: string
    value: ConversationTurn
    indexes: {
      'by-conversation': string
      'by-timestamp': Date
    }
  }
  documents: {
    key: string
    value: CompanyDocument
    indexes: {
      'by-type': string
      'by-category': string
      'by-updated': string
    }
  }
  kv: {
    key: string
    value: { key: string; value: unknown }
  }
  outbox: {
    key: string // opId
    value: OutboxOp
    indexes: { 'by-ts': Date }
  }
}

export class ConversationManager {
  private db: IDBPDatabase<ConversationDB> | null = null
  private currentConversationId: string | null = null
  private maxHistoryTurns: number = 50
  private deviceId: string | null = null
  private lamport: number = 0
  private syncTransport?: SyncTransport
  private syncBaseUrl?: string

  constructor(maxHistoryTurns = 50, options: ConversationManagerOptions = {}) {
    this.maxHistoryTurns = maxHistoryTurns
    const defaultTransport: SyncTransport | undefined = typeof fetch !== 'undefined'
      ? (input, init) => fetch(input, init as RequestInit)
      : undefined
    const defaultBase = typeof location !== 'undefined'
      ? `${location.origin}/api`
      : undefined

    this.syncTransport = options.syncTransport ?? defaultTransport
    this.syncBaseUrl = options.syncBaseUrl ?? defaultBase
  }

  configureSync(options: ConversationManagerOptions): void {
    if (options.syncTransport) {
      this.syncTransport = options.syncTransport
    }
    if (options.syncBaseUrl) {
      this.syncBaseUrl = options.syncBaseUrl
    }
  }

  private resolveSyncUrl(path: string): string {
    if (!this.syncBaseUrl) {
      throw new Error('Sync base URL not configured for ConversationManager')
    }

    const ensureTrailingSlash = (value: string): string =>
      value.endsWith('/') ? value : `${value}/`

    const normalizedPath = path.replace(/^\/+/, '')
    let baseString = this.syncBaseUrl

    if (!/^https?:\/\//i.test(baseString)) {
      if (typeof window === 'undefined' || !window.location) {
        throw new Error(`Relative sync base URL requires browser environment: ${baseString}`)
      }
      const origin = `${window.location.protocol}//${window.location.host}`
      baseString = new URL(baseString, origin).toString()
    }

    return new URL(normalizedPath, ensureTrailingSlash(baseString)).toString()
  }

  async initialize(contextName: string = 'default'): Promise<void> {
    this.db = await openDB<ConversationDB>(`JarvisVoiceAI-${contextName}`, 3, {
      upgrade(db, oldVersion) {
        // Conversations store
        if (!db.objectStoreNames.contains('conversations')) {
          db.createObjectStore('conversations', { keyPath: 'id' })
        }

        // Turns store with indexes
        if (!db.objectStoreNames.contains('turns')) {
          const turnsStore = db.createObjectStore('turns', { keyPath: 'id' })
          turnsStore.createIndex('by-conversation', 'conversationId')
          turnsStore.createIndex('by-timestamp', 'timestamp')
        }

        // Documents store with indexes (version 2+)
        if (oldVersion < 2 && !db.objectStoreNames.contains('documents')) {
          const documentsStore = db.createObjectStore('documents', { keyPath: 'id' })
          documentsStore.createIndex('by-type', 'metadata.type')
          documentsStore.createIndex('by-category', 'metadata.category')
          documentsStore.createIndex('by-updated', 'metadata.lastUpdated')
        }

        // KV and Outbox (version 3+)
        if (oldVersion < 3) {
          if (!db.objectStoreNames.contains('kv')) {
            db.createObjectStore('kv', { keyPath: 'key' })
          }
          if (!db.objectStoreNames.contains('outbox')) {
            const outbox = db.createObjectStore('outbox', { keyPath: 'opId' })
            outbox.createIndex('by-ts', 'ts')
          }
        }
      },
    })

    // Initialize deviceId and lamport clock
    this.deviceId = (await this.getKV<string>('device_id')) || null
    if (!this.deviceId) {
      this.deviceId = `dev_${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`
      await this.setKV('device_id', this.deviceId)
    }
    this.lamport = (await this.getKV<number>('lamport')) || 0
  }

  async createNewConversation(name?: string): Promise<string> {
    if (!this.db) throw new Error('ConversationManager not initialized')

    const id = `conv_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
    const conversation = {
      id,
      name: name || `Conversation ${new Date().toLocaleString()}`,
      createdAt: new Date(),
      updatedAt: new Date(),
      turns: []
    }

    await this.db.put('conversations', conversation)
    this.currentConversationId = id
    return id
  }

  async getConversation(conversationId: string): Promise<{id: string, name: string, createdAt: Date, updatedAt: Date} | null> {
    if (!this.db) throw new Error('ConversationManager not initialized')
    const conversation = await this.db.get('conversations', conversationId)
    if (!conversation) return null
    return {
      id: conversation.id,
      name: conversation.name,
      createdAt: conversation.createdAt,
      updatedAt: conversation.updatedAt
    }
  }

  async renameConversation(conversationId: string, name: string): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')
    const trimmed = name.trim()
    if (!trimmed) throw new Error('Conversation name cannot be empty')

    const conversation = await this.db.get('conversations', conversationId)
    if (!conversation) throw new Error('Conversation not found')

    conversation.name = trimmed
    conversation.updatedAt = new Date()
    await this.db.put('conversations', conversation)
  }

  async addTurn(turn: ConversationTurn): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')
    if (!this.currentConversationId) {
      await this.createNewConversation()
    }

    // Add conversation reference to turn
    const turnWithConversation = {
      ...turn,
      conversationId: this.currentConversationId!
    }

    // Store turn in IndexedDB
    await this.db.put('turns', turnWithConversation)

    // Update conversation metadata
    const conversation = await this.db.get('conversations', this.currentConversationId!)
    if (conversation) {
      conversation.updatedAt = new Date()
      await this.db.put('conversations', conversation)
    }

    // Cleanup old turns if we exceed max history
    await this.cleanupOldTurns()
  }

  // Outbox helpers --------------------------------------------------------
  private nextLamport(): number {
    this.lamport = Math.max(this.lamport, Date.now()) + 1
    // best-effort persist; ignore errors
    this.setKV('lamport', this.lamport).catch(() => {})
    return this.lamport
  }

  private getRoleFromTurn(turn: ConversationTurn): TurnRole {
    if (turn.userTranscript) return 'user'
    if (turn.assistantResponse || turn.assistantText) return 'assistant'
    return 'tool'
  }

  async queueAppendTurnOp(turn: ConversationTurn): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')
    const deviceId = this.deviceId || 'dev_unknown'
    const op: OutboxOp = {
      opId: `op_${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`,
      deviceId,
      type: 'append_turn',
      lamport: this.nextLamport(),
      ts: new Date(),
      body: {
        conversationId: this.currentConversationId,
        turn: {
          id: turn.id,
          role: this.getRoleFromTurn(turn),
          timestamp: turn.timestamp,
          userTranscript: turn.userTranscript,
          assistantResponse: turn.assistantResponse
        }
      }
    }
    await this.db.put('outbox', op)
  }

  private async listOutbox(limit = 500): Promise<OutboxOp[]> {
    if (!this.db) return []
    const all = await this.db.getAllFromIndex('outbox', 'by-ts')
    return all.slice(0, limit)
  }

  private async deleteOutbox(opIds: string[]): Promise<void> {
    if (!this.db || opIds.length === 0) return
    const tx = this.db.transaction('outbox', 'readwrite')
    for (const id of opIds) await tx.objectStore('outbox').delete(id)
    await tx.done
  }

  async pushOutbox(): Promise<number> {
    if (!this.db) return 0
    const ops = await this.listOutbox()
    if (ops.length === 0) return 0

    if (!this.syncTransport || !this.syncBaseUrl) {
      throw new Error('Sync transport not configured for ConversationManager')
    }

    const cursor = (await this.getKV<number>('server_cursor')) || 0
    const deviceId = this.deviceId || 'dev_unknown'

    // Serialize Dates for transport
    const payload = {
      deviceId,
      cursor,
      ops: ops.map(o => ({ ...o, ts: o.ts.toISOString() }))
    }

    const resp = await this.syncTransport(this.resolveSyncUrl('sync/push'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    if (!resp.ok) throw new Error(`push failed: ${resp.status}`)
    const json = await resp.json() as { acked?: string[]; nextCursor?: number }
    const acked: string[] = json.acked || []
    const nextCursor: number = json.nextCursor || cursor
    await this.deleteOutbox(acked)
    await this.setKV('server_cursor', nextCursor)
    return acked.length
  }

  async pullAndApplyOps(): Promise<number> {
    if (!this.syncTransport || !this.syncBaseUrl) {
      throw new Error('Sync transport not configured for ConversationManager')
    }

    const cursor = (await this.getKV<number>('server_cursor')) || 0
    const resp = await this.syncTransport(
      `${this.resolveSyncUrl('sync/pull')}?cursor=${cursor}`
    )
    if (!resp.ok) throw new Error(`pull failed: ${resp.status}`)
    const json = await resp.json() as { ops?: any[]; nextCursor?: number }
    const ops: OutboxOp[] = (json.ops || []).map((o: any) => ({ ...o, ts: new Date(o.ts) }))
    const nextCursor: number = json.nextCursor ?? cursor

    if (ops.length > 0 && this.db) {
      // Apply ops in order (append_turn only for now)
      for (const op of ops) {
        if (op.type === 'append_turn') {
          const body = op.body as { turn?: any; conversationId?: string }
          const t = body?.turn
          if (!t?.id || !body?.conversationId) continue
          const turn: ConversationTurn = {
            id: t.id,
            timestamp: new Date(t.timestamp || op.ts),
            conversationId: body.conversationId,
            userTranscript: t.userTranscript,
            assistantResponse: t.assistantResponse
          }
          // Upsert turn
          await this.db.put('turns', turn)
        }
      }
    }

    await this.setKV('server_cursor', nextCursor)
    return ops.length
  }

  private async cleanupOldTurns(): Promise<void> {
    if (!this.db || !this.currentConversationId) return

    const turns = await this.db.getAllFromIndex('turns', 'by-conversation', this.currentConversationId)
    if (turns.length > this.maxHistoryTurns) {
      // Sort by timestamp and delete oldest
      turns.sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime())
      const turnsToDelete = turns.slice(0, turns.length - this.maxHistoryTurns)

      const tx = this.db.transaction('turns', 'readwrite')
      for (const turn of turnsToDelete) {
        await tx.objectStore('turns').delete(turn.id)
      }
      await tx.done
    }
  }

  async getConversationHistory(): Promise<ConversationTurn[]> {
    if (!this.db || !this.currentConversationId) return []

    const turns = await this.db.getAllFromIndex('turns', 'by-conversation', this.currentConversationId)
    return turns.sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime())
  }

  async getCurrentConversationId(): Promise<string | null> {
    return this.currentConversationId
  }

  async switchToConversation(conversationId: string): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')

    const conversation = await this.db.get('conversations', conversationId)
    if (!conversation) throw new Error('Conversation not found')

    this.currentConversationId = conversationId
  }

  async getAllConversations(): Promise<Array<{id: string, name: string, createdAt: Date, updatedAt: Date}>> {
    if (!this.db) return []

    const conversations = await this.db.getAll('conversations')
    return conversations.sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime())
  }

  async deleteConversation(conversationId: string): Promise<void> {
    if (!this.db) return

    // Delete all turns for this conversation
    const turns = await this.db.getAllFromIndex('turns', 'by-conversation', conversationId)
    const tx = this.db.transaction(['conversations', 'turns'], 'readwrite')

    for (const turn of turns) {
      await tx.objectStore('turns').delete(turn.id)
    }
    await tx.objectStore('conversations').delete(conversationId)
    await tx.done

    if (this.currentConversationId === conversationId) {
      this.currentConversationId = null
    }
  }

  /**
   * Delete all conversations and their turns, preserving documents.
   */
  async clearAllConversations(): Promise<void> {
    if (!this.db) return
    const tx = this.db.transaction(['conversations', 'turns'], 'readwrite')
    await tx.objectStore('conversations').clear()
    await tx.objectStore('turns').clear()
    await tx.done
    this.currentConversationId = null
  }

  async getStats(): Promise<{conversationCount: number, turnCount: number, totalSize: number}> {
    if (!this.db) return {conversationCount: 0, turnCount: 0, totalSize: 0}

    const conversations = await this.db.count('conversations')
    const turns = await this.db.count('turns')

    // Rough size estimation (this would need more sophisticated calculation in real app)
    const totalSize = turns * 1024 // Estimate 1KB per turn

    return {
      conversationCount: conversations,
      turnCount: turns,
      totalSize
    }
  }

  async exportData(): Promise<string> {
    if (!this.db) return '{}'

    const conversations = await this.db.getAll('conversations')
    const turns = await this.db.getAll('turns')
    const documents = await this.db.getAll('documents')

    return JSON.stringify({
      conversations,
      turns,
      documents,
      exportedAt: new Date().toISOString()
    }, null, 2)
  }

  async clearAllData(): Promise<void> {
    if (!this.db) return

    const tx = this.db.transaction(['conversations', 'turns', 'documents'], 'readwrite')
    await tx.objectStore('conversations').clear()
    await tx.objectStore('turns').clear()
    await tx.objectStore('documents').clear()
    await tx.done

    this.currentConversationId = null
  }

  // KV helpers ------------------------------------------------------------
  async setKV(key: string, value: unknown): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')
    await this.db.put('kv', { key, value })
  }

  async getKV<T = unknown>(key: string): Promise<T | undefined> {
    if (!this.db) return undefined
    const res = await this.db.get('kv', key)
    return res?.value as T | undefined
  }

  // Document management for RAG
  async addDocument(document: CompanyDocument): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')
    await this.db.put('documents', document)
  }

  async getAllDocuments(): Promise<CompanyDocument[]> {
    if (!this.db) return []
    return await this.db.getAll('documents')
  }

  async searchDocuments(type?: string, category?: string): Promise<CompanyDocument[]> {
    if (!this.db) return []

    if (type) {
      return await this.db.getAllFromIndex('documents', 'by-type', type)
    }
    if (category) {
      return await this.db.getAllFromIndex('documents', 'by-category', category)
    }

    return await this.db.getAll('documents')
  }

  async loadSampleData(): Promise<void> {
    if (!this.db) throw new Error('ConversationManager not initialized')

    // Check if we already have sample data
    const existingDocs = await this.db.count('documents')
    if (existingDocs > 0) return

    // Load sample company documents for RAG
    const sampleDocuments: CompanyDocument[] = [
      {
        id: 'doc_1',
        content: 'Q3 2024 revenue increased 15% year-over-year to $2.3B, driven by strong performance in cloud services and AI products.',
        embedding: new Array(1536).fill(0).map(() => Math.random()), // Mock embedding
        metadata: {
          source: 'earnings-report-q3-2024.pdf',
          type: 'financial',
          lastUpdated: '2024-10-15',
          category: 'earnings',
          quarter: 'Q3-2024'
        }
      },
      {
        id: 'doc_2',
        content: 'Our new AI-powered analytics platform helps customers reduce processing time by 40% and improve accuracy by 25%.',
        embedding: new Array(1536).fill(0).map(() => Math.random()),
        metadata: {
          source: 'product-specs-analytics-v2.md',
          type: 'product',
          lastUpdated: '2024-09-20',
          category: 'analytics',
          product: 'analytics-platform'
        }
      },
      {
        id: 'doc_3',
        content: 'Remote work policy: Employees can work from home up to 3 days per week. Flexible hours between 7 AM - 7 PM with core hours 10 AM - 3 PM.',
        embedding: new Array(1536).fill(0).map(() => Math.random()),
        metadata: {
          source: 'employee-handbook-2024.pdf',
          type: 'policy',
          lastUpdated: '2024-01-15',
          category: 'hr-policies',
          department: 'human-resources'
        }
      }
    ]

    for (const doc of sampleDocuments) {
      await this.addDocument(doc)
    }
  }
}
