/**
 * Session Bootstrap Tests
 *
 * Tests for the SSOT bootstrap module that ensures UI and Realtime
 * receive the same history data from a single query.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ConversationTurn } from '@jarvis/data-local'
import type { RealtimeMessageItem } from '@openai/agents/realtime'

// Mock the session-handler module
vi.mock('../lib/session-handler', () => ({
  sessionHandler: {
    connectWithHistory: vi.fn().mockResolvedValue({
      session: { updateHistory: vi.fn() },
      agent: { name: 'Test Agent' },
    }),
  },
}))

// Mock the history-mapper module
vi.mock('../lib/history-mapper', () => ({
  mapConversationToRealtimeItems: vi.fn((turns: ConversationTurn[]): RealtimeMessageItem[] => {
    // Simple mock that creates one item per turn with userTranscript
    const items: RealtimeMessageItem[] = []
    for (const turn of turns) {
      if (turn.userTranscript) {
        items.push({
          type: 'message',
          role: 'user',
          itemId: `user-${turn.id}`,
          status: 'completed',
          content: [{ type: 'input_text', text: turn.userTranscript }],
        } as RealtimeMessageItem)
      }
      if (turn.assistantResponse) {
        items.push({
          type: 'message',
          role: 'assistant',
          itemId: `asst-${turn.id}`,
          status: 'completed',
          content: [{ type: 'output_text', text: turn.assistantResponse }],
        } as RealtimeMessageItem)
      }
    }
    return items
  }),
  trimForRealtime: vi.fn((turns: ConversationTurn[], maxTurns: number) => {
    return turns.slice(-maxTurns)
  }),
}))

// Import after mocks are set up
import { bootstrapSession, type BootstrapResult, type BootstrapOptions } from '../lib/session-bootstrap'
import { sessionHandler } from '../lib/session-handler'
import { mapConversationToRealtimeItems, trimForRealtime } from '../lib/history-mapper'

describe('Session Bootstrap (SSOT)', () => {
  const mockTurns: ConversationTurn[] = [
    {
      id: 'turn-1',
      timestamp: new Date('2024-01-01T10:00:00Z'),
      userTranscript: 'Hello',
      assistantResponse: 'Hi there!',
    },
    {
      id: 'turn-2',
      timestamp: new Date('2024-01-01T10:01:00Z'),
      userTranscript: 'How are you?',
      assistantResponse: 'I am doing well!',
    },
    {
      id: 'turn-3',
      timestamp: new Date('2024-01-01T10:02:00Z'),
      userTranscript: 'Tell me a joke',
      assistantResponse: 'Why did the chicken cross the road?',
    },
  ]

  const mockSessionManager = {
    getConversationManager: vi.fn().mockReturnValue({
      getCurrentConversationId: vi.fn().mockResolvedValue('conv-123'),
    }),
    getConversationHistory: vi.fn().mockResolvedValue(mockTurns),
  }

  const mockOptions: BootstrapOptions = {
    context: { name: 'Test Agent', instructions: 'Be helpful' },
    sessionManager: mockSessionManager as any,
    onTokenRequest: vi.fn().mockResolvedValue('mock-token'),
    realtimeHistoryTurns: 8,
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('bootstrapSession', () => {
    it('loads history exactly once from IndexedDB', async () => {
      await bootstrapSession(mockOptions)

      // History should be fetched exactly once
      expect(mockSessionManager.getConversationHistory).toHaveBeenCalledTimes(1)
    })

    it('returns the same history data for UI consumption', async () => {
      const result = await bootstrapSession(mockOptions)

      // Result should contain the full history for UI
      expect(result.history).toEqual(mockTurns)
      expect(result.history.length).toBe(3)
    })

    it('trims history for Realtime API based on realtimeHistoryTurns', async () => {
      await bootstrapSession(mockOptions)

      // trimForRealtime should be called with the full history and configured limit
      expect(trimForRealtime).toHaveBeenCalledWith(mockTurns, 8)
    })

    it('maps trimmed history to Realtime items', async () => {
      await bootstrapSession(mockOptions)

      // mapConversationToRealtimeItems should be called with trimmed history
      expect(mapConversationToRealtimeItems).toHaveBeenCalled()
    })

    it('passes history items to connectWithHistory (not re-querying)', async () => {
      await bootstrapSession(mockOptions)

      // sessionHandler.connectWithHistory should receive the pre-loaded items
      expect(sessionHandler.connectWithHistory).toHaveBeenCalledWith(
        expect.objectContaining({
          historyItems: expect.any(Array),
          context: mockOptions.context,
        })
      )
    })

    it('returns conversation ID from session manager', async () => {
      const result = await bootstrapSession(mockOptions)

      expect(result.conversationId).toBe('conv-123')
    })

    it('returns session and agent from handler', async () => {
      const result = await bootstrapSession(mockOptions)

      expect(result.session).toBeDefined()
      expect(result.agent).toBeDefined()
    })

    it('returns count of hydrated items', async () => {
      const result = await bootstrapSession(mockOptions)

      // Each turn with userTranscript + assistantResponse = 2 items per turn
      // 3 turns * 2 = 6 items
      expect(result.hydratedItemCount).toBe(6)
    })

    it('handles empty history gracefully', async () => {
      mockSessionManager.getConversationHistory.mockResolvedValueOnce([])

      const result = await bootstrapSession(mockOptions)

      expect(result.history).toEqual([])
      expect(result.hydratedItemCount).toBe(0)
    })

    it('uses default realtimeHistoryTurns of 8 when not specified', async () => {
      const optionsWithoutLimit = { ...mockOptions }
      delete (optionsWithoutLimit as any).realtimeHistoryTurns

      await bootstrapSession(optionsWithoutLimit)

      expect(trimForRealtime).toHaveBeenCalledWith(mockTurns, 8)
    })

    it('respects custom realtimeHistoryTurns setting', async () => {
      const customOptions = { ...mockOptions, realtimeHistoryTurns: 4 }

      await bootstrapSession(customOptions)

      expect(trimForRealtime).toHaveBeenCalledWith(mockTurns, 4)
    })
  })

  describe('SSOT guarantees', () => {
    it('UI and Realtime receive data from the same single query', async () => {
      const result = await bootstrapSession(mockOptions)

      // Verify single query
      expect(mockSessionManager.getConversationHistory).toHaveBeenCalledTimes(1)

      // Verify UI gets full history
      expect(result.history).toHaveLength(3)

      // Verify Realtime gets mapped items from same data
      const mappedCalls = vi.mocked(mapConversationToRealtimeItems).mock.calls
      expect(mappedCalls.length).toBe(1)

      // The data passed to mapper should be derived from the same query result
      // (after trimming)
      const trimmedCalls = vi.mocked(trimForRealtime).mock.calls
      expect(trimmedCalls[0][0]).toEqual(mockTurns)
    })
  })
})
