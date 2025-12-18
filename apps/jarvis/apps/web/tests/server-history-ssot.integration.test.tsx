import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AppProvider } from '../src/context'
import App from '../src/App'
import { appController } from '../lib/app-controller'

function getUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.toString()
  // Request
  return (input as any).url ? String((input as any).url) : String(input)
}

function jsonResponse(body: any, init?: Partial<{ status: number; statusText: string }>): Response {
  const status = init?.status ?? 200
  const statusText = init?.statusText ?? 'OK'
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: async () => body,
    headers: new Headers({ 'content-type': 'application/json' }),
  } as any as Response
}

describe('Server SSOT (history + clear survives refresh)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    // Ensure singleton controller doesn't carry state across tests
    appController.resetForTests()

    // Minimal audio mocks (App mounts voice controls)
    if (!(globalThis as any).MediaStream) {
      ;(globalThis as any).MediaStream = class MockMediaStream {}
    }

    // Default fetch mock: history returns one user + one assistant message.
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = getUrl(input)
      const method = (init?.method || 'GET').toUpperCase()

      if (url.includes('/api/jarvis/history') && method === 'GET') {
        return jsonResponse({
          messages: [
            { role: 'user', content: 'Hi', timestamp: new Date('2024-01-01T00:00:00Z').toISOString() },
            { role: 'assistant', content: 'Hello!', timestamp: new Date('2024-01-01T00:00:01Z').toISOString() },
          ],
          total: 2,
        })
      }

      if (url.includes('/api/jarvis/history') && method === 'DELETE') {
        return jsonResponse({}, { status: 200, statusText: 'OK' })
      }

      // bootstrap/tool endpoints can be called; keep them non-fatal
      if (url.includes('/api/jarvis/bootstrap') && method === 'GET') {
        return jsonResponse({ prompt: '', enabled_tools: [] })
      }

      // Context manifest fetch (jsdom doesn't serve static assets)
      if (url.includes('./contexts/personal/manifest.json') && method === 'GET') {
        return jsonResponse({
          version: '1.0.0',
          name: 'personal',
          description: 'Personal AI assistant context',
          configFile: 'config.ts',
          themeFile: 'theme.css',
          requiredEnvVars: [],
        })
      }

      // Anything else: return 200 to keep tests focused
      return jsonResponse({})
    })

    vi.stubGlobal('fetch', fetchMock as any)
  })

  it('loads messages from server history on mount', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    await waitFor(() => {
      expect(screen.getByText('Hi')).toBeDefined()
      expect(screen.getByText('Hello!')).toBeDefined()
    })
  })

  it('Clear All clears server history and refresh does not repopulate', async () => {
    const { unmount } = render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    // Wait for initial history to appear
    await waitFor(() => {
      expect(screen.getByText('Hi')).toBeDefined()
    })

    // Clear all
    fireEvent.click(screen.getByText('Clear All'))

    // UI should clear messages
    await waitFor(() => {
      expect(screen.queryByText('Hi')).toBeNull()
      expect(screen.queryByText('Hello!')).toBeNull()
    })

    // Update fetch mock: server history is empty after delete
    const fetchMock = vi.mocked(globalThis.fetch as any)
    fetchMock.mockImplementationOnce(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = getUrl(input)
      const method = (init?.method || 'GET').toUpperCase()
      if (url.includes('/api/jarvis/history') && method === 'GET') {
        return jsonResponse({ messages: [], total: 0 })
      }
      return jsonResponse({})
    })

    // Simulate refresh by unmount + remount
    unmount()
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    // Messages should stay empty
    await waitFor(() => {
      expect(screen.queryByText('Hi')).toBeNull()
      expect(screen.queryByText('Hello!')).toBeNull()
    })
  })
})
