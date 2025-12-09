/**
 * Text Channel Persistence Tests
 *
 * Tests that text messages are persisted to IndexedDB via conversationController.
 * This is the Phase 2 fix for the SSOT history refactor.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useTextChannel } from '../src/hooks/useTextChannel'
import { conversationController } from '../lib/conversation-controller'
import { appController } from '../lib/app-controller'

// Mock the appController
vi.mock('../lib/app-controller', () => ({
  appController: {
    sendText: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock conversationController
vi.mock('../lib/conversation-controller', () => ({
  conversationController: {
    addUserTurn: vi.fn().mockResolvedValue(true), // Returns true on successful persistence
    addAssistantTurn: vi.fn().mockResolvedValue(true),
  },
}))

// Mock context
const mockState = {
  messages: [],
  streamingContent: '',
  isConnected: true,
  sidebarOpen: false,
  voiceStatus: 'idle' as const,
  voiceMode: 'push-to-talk' as const,
  conversations: [],
  conversationId: 'test-conv-123',
  userTranscriptPreview: '',
}

const mockDispatch = vi.fn()

vi.mock('../src/context', () => ({
  useAppState: () => mockState,
  useAppDispatch: () => mockDispatch,
}))

describe('Text Channel Persistence', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockState.messages = []
    mockState.isConnected = true
  })

  describe('sendMessage', () => {
    it('should persist user message to IndexedDB', async () => {
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('Hello from text')
      })

      // Should call conversationController.addUserTurn to persist
      expect(conversationController.addUserTurn).toHaveBeenCalledWith('Hello from text')
    })

    it('should persist user message before sending to backend', async () => {
      const callOrder: string[] = []

      vi.mocked(conversationController.addUserTurn).mockImplementation(async () => {
        callOrder.push('addUserTurn')
      })

      vi.mocked(appController.sendText).mockImplementation(async () => {
        callOrder.push('sendText')
      })

      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('Test message')
      })

      // User turn should be persisted before sending to backend
      expect(callOrder).toEqual(['addUserTurn', 'sendText'])
    })

    it('should not persist empty messages', async () => {
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('')
        await result.current.sendMessage('   ')
      })

      expect(conversationController.addUserTurn).not.toHaveBeenCalled()
    })

    it('should trim message before persisting', async () => {
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('  Hello with spaces  ')
      })

      expect(conversationController.addUserTurn).toHaveBeenCalledWith('Hello with spaces')
    })

    it('should dispatch ADD_MESSAGE to React state', async () => {
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('Hello')
      })

      expect(mockDispatch).toHaveBeenCalledWith({
        type: 'ADD_MESSAGE',
        message: expect.objectContaining({
          role: 'user',
          content: 'Hello',
        }),
      })
    })

    it('should handle persistence errors gracefully', async () => {
      const error = new Error('IndexedDB write failed')
      vi.mocked(conversationController.addUserTurn).mockRejectedValueOnce(error)

      const onError = vi.fn()
      const { result } = renderHook(() => useTextChannel({ onError }))

      await act(async () => {
        await result.current.sendMessage('Test')
      })

      // Should still have attempted to persist
      expect(conversationController.addUserTurn).toHaveBeenCalled()
      // Error should be surfaced
      expect(onError).toHaveBeenCalled()
    })
  })

  describe('SSOT compliance', () => {
    it('should ensure text and voice messages use same persistence path', async () => {
      // Text messages should use conversationController.addUserTurn just like voice does
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('Text message')
      })

      // conversationController.addUserTurn is the same method used by voice path
      expect(conversationController.addUserTurn).toHaveBeenCalledTimes(1)
      expect(conversationController.addUserTurn).toHaveBeenCalledWith('Text message')
    })
  })
})
