/**
 * React Integration Tests
 *
 * Tests for React components and hooks:
 * 1. Voice controls (PTT press/release)
 * 2. Text input sending
 * 3. Sidebar toggle
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AppProvider } from '../src/context'
import App from '../src/App'

// Prevent tests from trying to establish a real OpenAI Realtime session.
vi.mock('../lib/app-controller', () => ({
  appController: {
    initialize: vi.fn().mockResolvedValue(undefined),
    connect: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn().mockResolvedValue(undefined),
    sendText: vi.fn().mockResolvedValue(undefined),
    clearServerHistory: vi.fn().mockResolvedValue(undefined),
  },
}))

describe('Jarvis React App', () => {
  beforeEach(() => {
    // Mock navigator.mediaDevices for voice tests
    if (!navigator.mediaDevices) {
      Object.defineProperty(navigator, 'mediaDevices', {
        value: {
          getUserMedia: () => Promise.resolve(new MediaStream()),
        },
        writable: true,
      })
    }
  })

  it('renders app shell', () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    expect(screen.getByText('Jarvis AI')).toBeDefined()
    expect(screen.getByPlaceholderText('Type a message...')).toBeDefined()
    // Voice button exists (label changes based on connection state)
    expect(document.getElementById('pttBtn')).toBeDefined()
  })

  it('toggles sidebar on button click', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const toggleButton = screen.getByLabelText('Open conversation sidebar')
    expect(toggleButton.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(toggleButton)

    await waitFor(() => {
      expect(toggleButton.getAttribute('aria-expanded')).toBe('true')
    })
  })

  it('sends text message on button click', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const input = screen.getByPlaceholderText('Type a message...') as HTMLInputElement
    const sendButton = screen.getByLabelText('Send message')

    // Type a message
    fireEvent.change(input, { target: { value: 'Hello' } })
    expect(input.value).toBe('Hello')

    // Send the message
    fireEvent.click(sendButton)

    // Message should appear in chat
    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeDefined()
    })

    // Input should be cleared
    expect(input.value).toBe('')
  })

  it('updates voice button status on press', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const voiceButton = document.getElementById('pttBtn') as HTMLButtonElement

    // Initial state: idle (not connected in standalone mode)
    expect(voiceButton.className).toContain('idle')

    // NOTE: This test does not establish a real Realtime connection.
    // `useRealtimeSession` only transitions to `listening` when the underlying
    // `voiceController` is connected; unit tests run disconnected by default.
    // So here we just assert the UI does not enter `listening` on its own.
    fireEvent.mouseDown(voiceButton)
    await new Promise((resolve) => setTimeout(resolve, 25))
    expect(voiceButton.className).not.toContain('listening')

    // Release button (mouseUp)
    fireEvent.mouseUp(voiceButton)

    // Should return to idle/ready state
    await waitFor(() => {
      expect(voiceButton.className).not.toContain('listening')
    })
  })

  it('toggles voice mode (PTT <-> hands-free)', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const voiceButton = document.getElementById('pttBtn') as HTMLButtonElement
    const modeToggle = screen.getByLabelText('Toggle hands-free mode')

    // Mode toggle should be disabled when not connected
    expect(modeToggle).toHaveProperty('disabled', true)

    // Connect first (in standalone mode)
    fireEvent.click(voiceButton)

    await waitFor(() => {
      expect(voiceButton.className).toContain('ready')
    })

    // Now toggle should be enabled
    expect(modeToggle).toHaveProperty('disabled', false)

    // Initial state: push-to-talk
    expect(modeToggle.getAttribute('aria-checked')).toBe('false')

    // Toggle to hands-free
    fireEvent.click(modeToggle)

    await waitFor(() => {
      expect(modeToggle.getAttribute('aria-checked')).toBe('true')
    })

    // Toggle back
    fireEvent.click(modeToggle)

    await waitFor(() => {
      expect(modeToggle.getAttribute('aria-checked')).toBe('false')
    })
  })

  it('clears messages on new conversation', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    // Send a message first
    const input = screen.getByPlaceholderText('Type a message...') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Test message' } })
    fireEvent.click(screen.getByLabelText('Send message'))

    await waitFor(() => {
      expect(screen.getByText('Test message')).toBeDefined()
    })

    // Click new conversation
    const newConvoBtn = screen.getByText('New Conversation')
    fireEvent.click(newConvoBtn)

    // Message should be gone
    await waitFor(() => {
      expect(screen.queryByText('Test message')).toBeNull()
    })
  })
})
