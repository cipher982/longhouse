import { describe, it, expect, beforeEach } from 'vitest'
import { ConversationManager } from '@jarvis/data-local/conversation-manager'
import type { ConversationTurn } from '@jarvis/data-local'

describe('ConversationManager', () => {
  let manager: ConversationManager

  beforeEach(async () => {
    manager = new ConversationManager()
    await manager.initialize()
  })

  it('should initialize successfully', async () => {
    expect(manager).toBeDefined()
    const stats = await manager.getStats()
    expect(stats.conversationCount).toBe(0)
    expect(stats.turnCount).toBe(0)
  })

  it('should create and track conversations', async () => {
    const conversationId = await manager.createNewConversation('Test Conversation')
    expect(conversationId).toBeDefined()
    expect(typeof conversationId).toBe('string')

    const currentId = await manager.getCurrentConversationId()
    expect(currentId).toBe(conversationId)

    const conversations = await manager.getAllConversations()
    expect(conversations).toHaveLength(1)
    expect(conversations[0].name).toBe('Test Conversation')
  })

  it('should add and retrieve conversation turns', async () => {
    const conversationId = await manager.createNewConversation()

    const turn: ConversationTurn = {
      id: 'test-turn-1',
      timestamp: new Date(),
      userTranscript: 'Hello, how are you?',
      conversationId
    }

    await manager.addTurn(turn)

    const history = await manager.getConversationHistory()
    expect(history).toHaveLength(1)
    expect(history[0].userTranscript).toBe('Hello, how are you?')
  })

  it('should load sample data for RAG', async () => {
    await manager.loadSampleData()
    const documents = await manager.getAllDocuments()
    expect(documents.length).toBeGreaterThan(0)

    // Verify sample document structure
    const doc = documents[0]
    expect(doc.id).toBeDefined()
    expect(doc.content).toBeDefined()
    expect(doc.embedding).toBeDefined()
    expect(doc.metadata).toBeDefined()
    expect(doc.metadata.type).toBeDefined()
  })
})
