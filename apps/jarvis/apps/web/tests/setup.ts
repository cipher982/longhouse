/**
 * Vitest setup file
 */

import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

// Provide a minimal MediaStream for tests that mock getUserMedia.
if (!(globalThis as any).MediaStream) {
  ;(globalThis as any).MediaStream = class MockMediaStream {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    constructor(_tracks?: any[]) {}
  }
}

// Default fetch mock for tests to avoid accidental network calls.
// Individual tests can override with vi.stubGlobal('fetch', ...) or global.fetch = ...
beforeEach(() => {
  if (typeof globalThis.fetch === 'function' && (globalThis.fetch as any).__vitestMocked) {
    return
  }

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String((input as any).url || input)
    const method = (init?.method || 'GET').toUpperCase()

    // Context manifest fetch
    if (url.includes('./contexts/personal/manifest.json') && method === 'GET') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({
          version: '1.0.0',
          name: 'personal',
          description: 'Personal AI assistant context',
          configFile: 'config.ts',
          themeFile: 'theme.css',
          requiredEnvVars: [],
        }),
      } as any as Response
    }

    // Jarvis bootstrap
    if (url.includes('/api/jarvis/bootstrap') && method === 'GET') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({ prompt: '', enabled_tools: [] }),
      } as any as Response
    }

    // Jarvis history default: empty
    if (url.includes('/api/jarvis/history') && method === 'GET') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({ messages: [], total: 0 }),
      } as any as Response
    }
    if (url.includes('/api/jarvis/history') && method === 'DELETE') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({}),
      } as any as Response
    }

    // Default OK
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({}),
    } as any as Response
  })

  ;(fetchMock as any).__vitestMocked = true
  vi.stubGlobal('fetch', fetchMock as any)
})

// Cleanup React components after each test
afterEach(() => {
  cleanup()
})

// Mock crypto.randomUUID if not available
if (!globalThis.crypto?.randomUUID) {
  Object.defineProperty(globalThis, 'crypto', {
    value: {
      ...globalThis.crypto,
      randomUUID: () => Math.random().toString(36).substring(2, 15),
    },
  })
}

// Mock import.meta.env for tests
const mockEnv = {}

Object.defineProperty(globalThis, 'import', {
  value: {
    meta: {
      env: mockEnv,
    },
  },
  writable: true,
  configurable: true,
})
