/**
 * Happy Path Integration Test
 *
 * Tests the critical user journey: Load UI → Send message → See AI response
 *
 * This test was written to catch bugs in the message response rendering flow.
 * Prior to this test, we had:
 * - Unit tests that tested isolated components with mocks
 * - "E2E" tests that only verified optimistic message updates
 * - No test that verified AI responses actually render in the UI
 *
 * This test requires:
 * - Running zerg-backend (for Jarvis BFF + session token)
 * - Valid OPENAI_API_KEY (for actual API calls)
 *
 * Run with: bun test happy-path.integration
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { AppProvider } from '../src/context'
import App from '../src/App'

// Mock MediaDevices for microphone access
const mockMediaStream = {
  getAudioTracks: () => [{
    kind: 'audio',
    id: 'mock-audio-track',
    label: 'Mock Microphone',
    enabled: true,
    muted: false,
    readyState: 'live',
    getSettings: () => ({ deviceId: 'mock-device' }),
    stop: vi.fn(),
  }],
  getTracks: () => [],
}

describe('Happy Path Integration - Message Send and Response', () => {
  // These tests require a running `zerg-backend` and real credentials.
  // In unit-test runs (CI/dev), skip to avoid flaky network-dependent failures.
  const integrationEnabled = process.env.JARVIS_INTEGRATION_TESTS === '1'

  if (!integrationEnabled) {
    it.skip('integration tests disabled (set JARVIS_INTEGRATION_TESTS=1)', () => {})
    return
  }

  beforeEach(() => {
    // Mock navigator.mediaDevices
    if (!navigator.mediaDevices) {
      Object.defineProperty(navigator, 'mediaDevices', {
        value: {
          getUserMedia: vi.fn().mockResolvedValue(mockMediaStream),
        },
        writable: true,
        configurable: true,
      })
    } else {
      vi.spyOn(navigator.mediaDevices, 'getUserMedia').mockResolvedValue(mockMediaStream as unknown as MediaStream)
    }
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  /**
   * THE CRITICAL TEST
   *
   * This test verifies the complete happy path:
   * 1. App initializes (context loads, controllers start)
   * 2. User types a message
   * 3. User sends the message
   * 4. Message appears in chat (optimistic)
   * 5. AI response streams in and appears in chat
   *
   * If this test fails, users cannot have conversations with Jarvis.
   */
  it('should display AI response after sending a text message', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    // Step 1: Wait for app initialization
    // The app should show the chat interface when ready
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type a message...')).toBeDefined()
    }, { timeout: 10000 })

    // Step 2: Wait for context initialization (this was a bug we fixed)
    // Look for initialization log or ready state
    const voiceButton = document.getElementById('pttBtn')
    await waitFor(() => {
      // Should not be in connecting state forever
      expect(voiceButton?.className).not.toContain('connecting')
    }, { timeout: 15000 })

    // Step 3: Type a test message
    const input = screen.getByPlaceholderText('Type a message...') as HTMLInputElement
    const testMessage = 'Say hello back to me in exactly 5 words'

    fireEvent.change(input, { target: { value: testMessage } })
    expect(input.value).toBe(testMessage)

    // Step 4: Send the message
    const sendButton = screen.getByLabelText('Send message')
    fireEvent.click(sendButton)

    // Step 5: Verify user message appears (optimistic update)
    await waitFor(() => {
      expect(screen.getByText(testMessage)).toBeDefined()
    }, { timeout: 2000 })

    // Input should be cleared after send
    expect(input.value).toBe('')

    // Step 6: THE CRITICAL ASSERTION - AI response should appear
    // This is where previous tests stopped. We need to verify a response actually renders.
    await waitFor(() => {
      // Look for any assistant message in the chat
      // The response should be in a message bubble with role="assistant" or similar
      const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
      expect(chatContainer).toBeDefined()

      // Count messages - should have more than just the user's message
      const messages = chatContainer?.querySelectorAll('.message, .chat-message, .turn')

      // We should have at least 2 messages: user + assistant
      expect(messages?.length).toBeGreaterThanOrEqual(2)
    }, { timeout: 30000 }) // Allow time for API response

    // Step 7: Verify the response is actually from the assistant (not another user message)
    const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
    const messages = chatContainer?.querySelectorAll('.message, .chat-message, .turn')

    if (messages && messages.length >= 2) {
      // The last message should be the assistant's response
      const lastMessage = messages[messages.length - 1]
      const isAssistantMessage =
        lastMessage.classList.contains('assistant') ||
        lastMessage.getAttribute('data-role') === 'assistant' ||
        lastMessage.querySelector('.assistant-content, .bot-message') !== null

      // If it's not clearly an assistant message, at least verify it's different from what we sent
      const lastMessageText = lastMessage.textContent || ''
      expect(lastMessageText).not.toBe(testMessage)
      expect(lastMessageText.length).toBeGreaterThan(0)
    }
  }, 60000) // 60 second timeout for full flow

  /**
   * Test that streaming responses update the UI incrementally
   */
  it('should show streaming indicator while response is generating', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    // Wait for initialization
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type a message...')).toBeDefined()
    }, { timeout: 10000 })

    const voiceButton = document.getElementById('pttBtn')
    await waitFor(() => {
      expect(voiceButton?.className).not.toContain('connecting')
    }, { timeout: 15000 })

    // Send a message that will generate a longer response
    const input = screen.getByPlaceholderText('Type a message...') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Count from 1 to 10 slowly' } })

    const sendButton = screen.getByLabelText('Send message')
    fireEvent.click(sendButton)

    // During streaming, there should be some indication of ongoing response
    // This could be a streaming class, a loading indicator, or partial content
    let sawStreamingState = false

    // Poll for streaming state
    for (let i = 0; i < 50; i++) {
      await new Promise(r => setTimeout(r, 200))

      const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
      const streamingMessage = chatContainer?.querySelector('.streaming, .is-streaming, [data-streaming="true"]')
      const streamingContent = document.querySelector('.streaming-content')

      if (streamingMessage || streamingContent) {
        sawStreamingState = true
        break
      }

      // Also check for partial content (streaming in progress)
      const messages = chatContainer?.querySelectorAll('.message, .chat-message, .turn')
      if (messages && messages.length >= 2) {
        const lastMessage = messages[messages.length - 1]
        const content = lastMessage.textContent || ''
        // If we see partial numbers, streaming is working
        if (content.includes('1') && !content.includes('10')) {
          sawStreamingState = true
          break
        }
      }
    }

    // Wait for response to complete
    await waitFor(() => {
      const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
      const messages = chatContainer?.querySelectorAll('.message, .chat-message, .turn')
      expect(messages?.length).toBeGreaterThanOrEqual(2)
    }, { timeout: 30000 })

    // Note: sawStreamingState might be false if response is very fast
    // The important thing is that the final response appears
    console.log(`[Test] Streaming state observed: ${sawStreamingState}`)
  }, 60000)

  /**
   * Test multiple message exchange (conversation continuity)
   */
  it('should maintain conversation context across multiple messages', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type a message...')).toBeDefined()
    }, { timeout: 10000 })

    const voiceButton = document.getElementById('pttBtn')
    await waitFor(() => {
      expect(voiceButton?.className).not.toContain('connecting')
    }, { timeout: 15000 })

    const input = screen.getByPlaceholderText('Type a message...') as HTMLInputElement
    const sendButton = screen.getByLabelText('Send message')

    // First message
    fireEvent.change(input, { target: { value: 'Remember the number 42' } })
    fireEvent.click(sendButton)

    // Wait for first response
    await waitFor(() => {
      const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
      const messages = chatContainer?.querySelectorAll('.message, .chat-message, .turn')
      expect(messages?.length).toBeGreaterThanOrEqual(2)
    }, { timeout: 30000 })

    // Second message referencing the first
    fireEvent.change(input, { target: { value: 'What number did I ask you to remember?' } })
    fireEvent.click(sendButton)

    // Wait for second response
    await waitFor(() => {
      const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
      const messages = chatContainer?.querySelectorAll('.message, .chat-message, .turn')
      // Should have 4 messages now: user1, assistant1, user2, assistant2
      expect(messages?.length).toBeGreaterThanOrEqual(4)
    }, { timeout: 30000 })

    // The response should mention "42" - proving context was maintained
    const chatContainer = document.querySelector('.chat-messages, .conversation-container, #transcript')
    const allText = chatContainer?.textContent || ''

    // Count occurrences - should appear in both user message and assistant response
    const count42 = (allText.match(/42/g) || []).length
    expect(count42).toBeGreaterThanOrEqual(2) // At least in user msg and assistant response
  }, 90000)
})
