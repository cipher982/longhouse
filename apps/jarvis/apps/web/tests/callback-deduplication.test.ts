/**
 * Callback Deduplication Tests
 *
 * Tests that session callbacks (onConnected, onTranscript, etc.) are only
 * called once per event, preventing duplicate UI updates and messages.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useRealtimeSession } from '../src/hooks/useRealtimeSession'
import { voiceController } from '../lib/voice-controller'
import { appController } from '../lib/app-controller'
import { stateManager } from '../lib/state-manager'

// Mock the controllers
vi.mock('../lib/voice-controller', () => ({
  voiceController: {
    addListener: vi.fn(),
    removeListener: vi.fn(),
    isConnected: vi.fn().mockReturnValue(false),
    startPTT: vi.fn(),
    stopPTT: vi.fn(),
    setHandsFree: vi.fn(),
    getState: vi.fn().mockReturnValue({ handsFree: false }),
  },
}))

vi.mock('../lib/app-controller', () => ({
  appController: {
    initialize: vi.fn(() => Promise.resolve()),
    connect: vi.fn(() => Promise.resolve()),
    disconnect: vi.fn(),
    setOnHistoryLoaded: vi.fn(),
  },
}))

vi.mock('../lib/state-manager', () => ({
  stateManager: {
    addListener: vi.fn(),
    removeListener: vi.fn(),
  },
}))

vi.mock('../lib/audio-controller', () => ({
  audioController: {
    initialize: vi.fn(),
  },
}))

vi.mock('../lib/session-handler', () => ({
  sessionHandler: {},
}))

vi.mock('../lib/conversation-controller', () => ({
  conversationController: {},
}))

// Mock context
const mockDispatch = vi.fn()
vi.mock('../src/context', () => ({
  useAppDispatch: () => mockDispatch,
}))

describe('Callback Deduplication', () => {
  let voiceListeners: Array<(event: any) => void>
  let stateListeners: Array<(event: any) => void>

  beforeEach(() => {
    vi.clearAllMocks()
    voiceListeners = []
    stateListeners = []

    // Capture listeners when they're added
    vi.mocked(voiceController.addListener).mockImplementation((listener) => {
      voiceListeners.push(listener)
    })
    vi.mocked(voiceController.removeListener).mockImplementation((listener) => {
      voiceListeners = voiceListeners.filter((l) => l !== listener)
    })
    vi.mocked(stateManager.addListener).mockImplementation((listener) => {
      stateListeners.push(listener)
    })
    vi.mocked(stateManager.removeListener).mockImplementation((listener) => {
      stateListeners = stateListeners.filter((l) => l !== listener)
    })

    // Mock DOM elements
    vi.spyOn(document, 'getElementById').mockImplementation((id) => {
      if (id === 'remoteAudio') return document.createElement('audio')
      if (id === 'pttBtn') return document.createElement('button')
      return null
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe('onConnected callback', () => {
    it('should only call onConnected once per connection', async () => {
      const onConnected = vi.fn()

      const { result } = renderHook(() =>
        useRealtimeSession({
          autoConnect: false, // Disable auto-connect for controlled testing
          onConnected,
        })
      )

      // Manually trigger connect
      await act(async () => {
        await result.current.connect()
      })

      // Simulate SESSION_CHANGED event (which also triggers onConnected in current buggy code)
      act(() => {
        stateListeners.forEach((listener) => {
          listener({ type: 'SESSION_CHANGED', session: {} })
        })
      })

      // BUG: Currently onConnected is called twice:
      // 1. From connect() callback after appController.connect() resolves
      // 2. From SESSION_CHANGED event listener
      // EXPECTED: Should only be called once
      expect(onConnected).toHaveBeenCalledTimes(1)
    })

    it('should only call onConnected once during auto-connect', async () => {
      const onConnected = vi.fn()

      renderHook(() =>
        useRealtimeSession({
          autoConnect: true,
          onConnected,
        })
      )

      // Wait for auto-connect to complete
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 150))
      })

      // Simulate SESSION_CHANGED event
      act(() => {
        stateListeners.forEach((listener) => {
          listener({ type: 'SESSION_CHANGED', session: {} })
        })
      })

      // EXPECTED: Should only be called once
      expect(onConnected).toHaveBeenCalledTimes(1)
    })
  })

  describe('onTranscript callback', () => {
    it('should only call onTranscript once per transcript event', async () => {
      const onTranscript = vi.fn()

      renderHook(() =>
        useRealtimeSession({
          autoConnect: false,
          onTranscript,
        })
      )

      // Wait for effect to register listener
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10))
      })

      // Simulate a single transcript event
      act(() => {
        voiceListeners.forEach((listener) => {
          listener({ type: 'transcript', text: 'Hello', isFinal: true })
        })
      })

      // Should only receive one callback per event
      expect(onTranscript).toHaveBeenCalledTimes(1)
      expect(onTranscript).toHaveBeenCalledWith('Hello', true)
    })

    it('should only register one voice controller listener', async () => {
      renderHook(() =>
        useRealtimeSession({
          autoConnect: false,
        })
      )

      // Wait for effect to register listener
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10))
      })

      // Should only have one listener registered
      expect(voiceListeners.length).toBe(1)
    })
  })

  describe('listener cleanup', () => {
    it('should properly clean up listeners on unmount', async () => {
      const { unmount } = renderHook(() =>
        useRealtimeSession({
          autoConnect: false,
        })
      )

      // Wait for effect to register listener
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10))
      })

      expect(voiceListeners.length).toBe(1)
      expect(stateListeners.length).toBe(1)

      // Unmount
      unmount()

      // Listeners should be removed
      expect(voiceListeners.length).toBe(0)
      expect(stateListeners.length).toBe(0)
    })
  })
})
