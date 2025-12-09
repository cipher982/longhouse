/**
 * Vitest setup file
 */

import 'fake-indexeddb/auto'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

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
const mockEnv = {
  VITE_JARVIS_ENABLE_REALTIME_BRIDGE: 'false',
}

Object.defineProperty(globalThis, 'import', {
  value: {
    meta: {
      env: mockEnv,
    },
  },
  writable: true,
  configurable: true,
})
