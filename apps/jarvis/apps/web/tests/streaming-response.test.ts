/**
 * Streaming Response Tests
 *
 * Tests that AI responses properly stream to the UI and finalize correctly.
 * This verifies the flow from transport events through to React state.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { conversationController } from '../lib/conversation-controller'
import { stateManager } from '../lib/state-manager'

// Don't mock these - test the real integration
vi.mock('@jarvis/core', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    conversation: vi.fn(),
    streamingResponse: vi.fn(),
  },
}))

describe('Streaming Response', () => {
  let stateChanges: any[]
  let listener: (event: any) => void

  beforeEach(() => {
    vi.clearAllMocks()
    stateChanges = []

    // Create a stable listener reference for proper cleanup
    listener = (event: any) => {
      stateChanges.push(event)
    }

    // Listen to state manager events
    stateManager.addListener(listener)

    // Clear any existing streaming state
    conversationController.clear()

    // Reset state manager to initial voice status
    stateManager.reset()
  })

  afterEach(() => {
    // Clean up listener using the same reference
    stateManager.removeListener(listener)
    conversationController.clear()
  })

  describe('appendStreaming', () => {
    it('should emit STREAMING_TEXT_CHANGED events as text accumulates', () => {
      // Clear events from setup
      stateChanges.length = 0

      conversationController.appendStreaming('Hello')
      conversationController.appendStreaming(' world')
      conversationController.appendStreaming('!')

      const streamingEvents = stateChanges.filter(
        (e) => e.type === 'STREAMING_TEXT_CHANGED'
      )

      expect(streamingEvents).toHaveLength(3)
      expect(streamingEvents[0].text).toBe('Hello')
      expect(streamingEvents[1].text).toBe('Hello world')
      expect(streamingEvents[2].text).toBe('Hello world!')
    })

    it('should auto-start streaming if not started', () => {
      // Before streaming
      expect(conversationController.isStreaming()).toBe(false)

      // First delta auto-starts
      conversationController.appendStreaming('Hi')

      expect(conversationController.isStreaming()).toBe(true)
    })
  })

  describe('finalizeStreaming', () => {
    it('should emit MESSAGE_FINALIZED with complete text', async () => {
      conversationController.appendStreaming('Hello ')
      conversationController.appendStreaming('there!')

      await conversationController.finalizeStreaming()

      const finalizedEvents = stateChanges.filter(
        (e) => e.type === 'MESSAGE_FINALIZED'
      )

      expect(finalizedEvents).toHaveLength(1)
      expect(finalizedEvents[0].message.content).toBe('Hello there!')
      expect(finalizedEvents[0].message.role).toBe('assistant')
    })

    it('should clear streaming text after finalization', async () => {
      conversationController.appendStreaming('Test')
      await conversationController.finalizeStreaming()

      // Should emit empty streaming text
      const lastStreamingEvent = stateChanges
        .filter((e) => e.type === 'STREAMING_TEXT_CHANGED')
        .pop()

      expect(lastStreamingEvent.text).toBe('')
    })

    it('should reset streaming state after finalization', async () => {
      conversationController.appendStreaming('Test')
      expect(conversationController.isStreaming()).toBe(true)

      await conversationController.finalizeStreaming()
      expect(conversationController.isStreaming()).toBe(false)
    })

    it('should be idempotent when not streaming', async () => {
      // Calling finalize when not streaming should be a no-op
      await conversationController.finalizeStreaming()
      await conversationController.finalizeStreaming()

      const finalizedEvents = stateChanges.filter(
        (e) => e.type === 'MESSAGE_FINALIZED'
      )

      // No finalize events since we weren't streaming
      expect(finalizedEvents).toHaveLength(0)
    })
  })

  describe('voice status integration', () => {
    it('should track voice status changes', () => {
      stateManager.setVoiceStatus('speaking')
      stateManager.setVoiceStatus('ready')

      const statusEvents = stateChanges.filter(
        (e) => e.type === 'VOICE_STATUS_CHANGED'
      )

      expect(statusEvents).toHaveLength(2)
      expect(statusEvents[0].status).toBe('speaking')
      expect(statusEvents[1].status).toBe('ready')
    })

    it('should not emit redundant status changes', () => {
      stateManager.setVoiceStatus('speaking')
      stateManager.setVoiceStatus('speaking') // Same status
      stateManager.setVoiceStatus('speaking') // Same status

      const statusEvents = stateChanges.filter(
        (e) => e.type === 'VOICE_STATUS_CHANGED'
      )

      // Should only emit once since status didn't change
      expect(statusEvents).toHaveLength(1)
    })
  })

  describe('response.done handling', () => {
    it('should finalize streaming and reset status on response completion', async () => {
      // Simulate streaming
      conversationController.appendStreaming('Response text')
      stateManager.setVoiceStatus('speaking')

      // Simulate response.done
      await conversationController.finalizeStreaming()
      stateManager.setVoiceStatus('ready')

      // Check events
      const finalizedEvents = stateChanges.filter(
        (e) => e.type === 'MESSAGE_FINALIZED'
      )
      const statusEvents = stateChanges.filter(
        (e) => e.type === 'VOICE_STATUS_CHANGED'
      )

      expect(finalizedEvents).toHaveLength(1)
      expect(statusEvents.pop()?.status).toBe('ready')
    })

    it('should reset status to ready even without streaming', () => {
      // Start in speaking state (from audio start)
      stateManager.setVoiceStatus('speaking')

      // response.done without streaming text (edge case)
      stateManager.setVoiceStatus('ready')

      const statusEvents = stateChanges.filter(
        (e) => e.type === 'VOICE_STATUS_CHANGED'
      )

      expect(statusEvents.pop()?.status).toBe('ready')
    })
  })
})
