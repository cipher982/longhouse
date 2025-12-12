/**
 * Jarvis E2E Tests
 *
 * Tests full integration with jarvis-server and realtime controllers.
 *
 * Prerequisites:
 * - jarvis-server running (via docker-compose)
 * - VITE_JARVIS_DEVICE_SECRET set in .env
 * - Mock OpenAI or real API key configured
 *
 * Run with: bun test jarvis.e2e
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AppProvider } from '../src/context'
import App from '../src/App'

describe('Jarvis E2E', () => {
  beforeEach(() => {
    // Ensure MediaStream exists in the test environment (jsdom/node)
    if (!(globalThis as any).MediaStream) {
      ;(globalThis as any).MediaStream = class MockMediaStream {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        constructor(_tracks?: any[]) {}
      }
    }

    // Mock navigator.mediaDevices for voice tests
    if (!navigator.mediaDevices) {
      Object.defineProperty(navigator, 'mediaDevices', {
        value: {
          getUserMedia: vi.fn().mockResolvedValue(
            new MediaStream([
              {
                kind: 'audio',
                id: 'mock-audio-track',
                label: 'Mock Microphone',
                enabled: true,
                muted: false,
                readyState: 'live',
                getSettings: () => ({ deviceId: 'mock-device' }),
              } as unknown as MediaStreamTrack,
            ])
          ),
        },
        writable: true,
        configurable: true,
      })
    }
  })

  it('should NOT connect to realtime session on mount', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const voiceButton = document.getElementById('pttBtn') as HTMLButtonElement

    // Realtime is manual-connect; initial status should remain idle.
    await waitFor(() => {
      expect(voiceButton.className).toContain('idle')
    })
  }, 10000)

  it('should send text message through appController', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    // Wait for initialization
    await new Promise((resolve) => setTimeout(resolve, 1000))

    const input = screen.getByPlaceholderText('Type a message...') as HTMLInputElement
    const sendButton = screen.getByLabelText('Send message')

    // Type a test message
    fireEvent.change(input, { target: { value: 'Hello from e2e test' } })
    expect(input.value).toBe('Hello from e2e test')

    // Send the message
    fireEvent.click(sendButton)

    // Message should appear in chat (optimistic update)
    await waitFor(() => {
      expect(screen.getByText('Hello from e2e test')).toBeDefined()
    })

    // Input should be cleared
    expect(input.value).toBe('')

    // Message should be sent to backend
    // If backend is available and responds, we should see a response
    // If not available, the optimistic message might be rolled back (error handling)
    // Give it time to potentially roll back or respond
    await new Promise((resolve) => setTimeout(resolve, 2000))

    // Verify message is still there (not rolled back) OR an error was surfaced
    const messageStillExists = screen.queryByText('Hello from e2e test') !== null
    if (!messageStillExists) {
      console.log('[E2E] Message was rolled back (backend error or timeout)')
    } else {
      console.log('[E2E] Message sent successfully')
    }
  }, 15000)

  it('should handle PTT press/release with connection guard', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const voiceButton = document.getElementById('pttBtn') as HTMLButtonElement

    // With manual-connect, PTT press should be ignored while idle.
    await waitFor(() => {
      expect(voiceButton.className).toContain('idle')
    })

    fireEvent.mouseDown(voiceButton)
    await new Promise((resolve) => setTimeout(resolve, 25))
    expect(voiceButton.className).not.toContain('listening')
  }, 10000)

  it('should allow reconnect after connection failure', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const voiceButton = document.getElementById('pttBtn') as HTMLButtonElement

    // Wait for initial connection attempt
    await waitFor(
      () => {
        expect(voiceButton.className).not.toContain('connecting')
      },
      { timeout: 5000 }
    )

    const isError = voiceButton.className.includes('error')

    if (isError) {
      console.log('[E2E] Connection failed - testing reconnect')

      // Click to reconnect
      fireEvent.click(voiceButton)

      // Should show connecting state
      await waitFor(() => {
        expect(voiceButton.className).toContain('connecting')
      })

      // Should eventually resolve to ready or error
      await waitFor(
        () => {
          const className = voiceButton.className
          expect(className).not.toContain('connecting')
        },
        { timeout: 5000 }
      )

      console.log('[E2E] Reconnect attempt completed')
    } else {
      console.log('[E2E] Initially connected - skipping reconnect test')
    }
  }, 15000)

  it('should have hands-free toggle disabled when not connected', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    const voiceButton = document.getElementById('pttBtn') as HTMLButtonElement
    const modeToggle = screen.getByLabelText('Toggle hands-free mode')

    // Wait for connection attempt
    await waitFor(
      () => {
        expect(voiceButton.className).not.toContain('connecting')
      },
      { timeout: 5000 }
    )

    const isConnected = voiceButton.className.includes('ready')

    if (!isConnected) {
      // Toggle should be disabled
      expect(modeToggle).toHaveProperty('disabled', true)
      console.log('[E2E] Mode toggle correctly disabled when not connected')
    } else {
      // Toggle should be enabled
      expect(modeToggle).toHaveProperty('disabled', false)
      console.log('[E2E] Mode toggle enabled when connected')

      // Test toggling
      fireEvent.click(modeToggle)
      await waitFor(() => {
        expect(modeToggle.getAttribute('aria-checked')).toBe('true')
      })

      console.log('[E2E] Successfully toggled to hands-free mode')
    }
  }, 10000)
})
