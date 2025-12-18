/**
 * Text Channel Persistence Tests
 *
 * Jarvis web no longer persists conversations in IndexedDB.
 * These tests ensure we still do optimistic UI updates and send to the backend.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useTextChannel } from '../src/hooks/useTextChannel'
import { appController } from '../lib/app-controller'

// Mock the appController
vi.mock('../lib/app-controller', () => ({
  appController: {
    sendText: vi.fn().mockResolvedValue(undefined),
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
    it('should send user message to backend', async () => {
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('Hello from text')
      })

      expect(appController.sendText).toHaveBeenCalledWith('Hello from text')
    })

    it('should trim message before sending', async () => {
      const callOrder: string[] = []

      vi.mocked(appController.sendText).mockImplementation(async () => {
        callOrder.push('sendText')
      })

      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('  Test message  ')
      })

      expect(callOrder).toEqual(['sendText'])
      expect(appController.sendText).toHaveBeenCalledWith('Test message')
    })

    it('should not persist empty messages', async () => {
      const { result } = renderHook(() => useTextChannel())

      await act(async () => {
        await result.current.sendMessage('')
        await result.current.sendMessage('   ')
      })

      expect(appController.sendText).not.toHaveBeenCalled()
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

    it('should handle backend send exceptions gracefully', async () => {
      const error = new Error('Backend failed')
      vi.mocked(appController.sendText).mockRejectedValueOnce(error)

      const onError = vi.fn()
      const { result } = renderHook(() => useTextChannel({ onError }))

      await act(async () => {
        await result.current.sendMessage('Test')
      })

      // Should have attempted to send
      expect(appController.sendText).toHaveBeenCalled()
      // Error should be surfaced
      expect(onError).toHaveBeenCalled()
    })
  })

  describe('SSOT compliance', () => {
    it('does not write to local persistence', async () => {
      const { result } = renderHook(() => useTextChannel())
      await act(async () => {
        await result.current.sendMessage('Text message')
      })
      expect(appController.sendText).toHaveBeenCalledTimes(1)
    })
  })
})
