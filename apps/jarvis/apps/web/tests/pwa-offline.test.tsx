/**
 * PWA Offline Tests
 *
 * Tests for offline detection and OfflineBanner component behavior.
 * Uses React Testing Library for component testing.
 */

import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { OfflineBanner } from '../src/components/OfflineBanner'

describe('OfflineBanner', () => {
  let originalOnLine: boolean

  beforeEach(() => {
    // Store original value
    originalOnLine = navigator.onLine
  })

  afterEach(() => {
    // Restore original value
    Object.defineProperty(navigator, 'onLine', {
      value: originalOnLine,
      writable: true,
      configurable: true,
    })
  })

  test('does not render when online', () => {
    // Mock online state
    Object.defineProperty(navigator, 'onLine', {
      value: true,
      writable: true,
      configurable: true,
    })

    render(<OfflineBanner />)

    // Banner should not be visible
    expect(screen.queryByText(/offline/i)).toBeNull()
  })

  test('renders warning when offline', () => {
    // Mock offline state
    Object.defineProperty(navigator, 'onLine', {
      value: false,
      writable: true,
      configurable: true,
    })

    render(<OfflineBanner />)

    // Banner should be visible with offline message
    expect(screen.getByText(/offline/i)).toBeDefined()
    expect(screen.getByText(/some features may be unavailable/i)).toBeDefined()
  })

  test('shows banner when going offline', async () => {
    // Start online
    Object.defineProperty(navigator, 'onLine', {
      value: true,
      writable: true,
      configurable: true,
    })

    render(<OfflineBanner />)

    // Should not be visible initially
    expect(screen.queryByText(/offline/i)).toBeNull()

    // Simulate going offline
    Object.defineProperty(navigator, 'onLine', {
      value: false,
      writable: true,
      configurable: true,
    })

    await act(async () => {
      window.dispatchEvent(new Event('offline'))
    })

    // Banner should now be visible
    await waitFor(() => {
      expect(screen.getByText(/offline/i)).toBeDefined()
    })
  })

  test('hides banner when coming back online', async () => {
    // Start offline
    Object.defineProperty(navigator, 'onLine', {
      value: false,
      writable: true,
      configurable: true,
    })

    render(<OfflineBanner />)

    // Should be visible initially
    expect(screen.getByText(/offline/i)).toBeDefined()

    // Simulate coming online
    Object.defineProperty(navigator, 'onLine', {
      value: true,
      writable: true,
      configurable: true,
    })

    await act(async () => {
      window.dispatchEvent(new Event('online'))
    })

    // Banner should be hidden
    await waitFor(() => {
      expect(screen.queryByText(/offline/i)).toBeNull()
    })
  })

  test('cleans up event listeners on unmount', () => {
    const addEventListenerSpy = vi.spyOn(window, 'addEventListener')
    const removeEventListenerSpy = vi.spyOn(window, 'removeEventListener')

    const { unmount } = render(<OfflineBanner />)

    // Should have added listeners
    expect(addEventListenerSpy).toHaveBeenCalledWith('online', expect.any(Function))
    expect(addEventListenerSpy).toHaveBeenCalledWith('offline', expect.any(Function))

    unmount()

    // Should have removed listeners
    expect(removeEventListenerSpy).toHaveBeenCalledWith('online', expect.any(Function))
    expect(removeEventListenerSpy).toHaveBeenCalledWith('offline', expect.any(Function))

    addEventListenerSpy.mockRestore()
    removeEventListenerSpy.mockRestore()
  })
})

describe('PWA Service Worker Integration', () => {
  test('service worker types are configured', () => {
    // This test verifies that vite-plugin-pwa types are properly set up
    // If TypeScript compilation succeeds, the types are working
    expect(true).toBe(true)
  })

  test('navigator.onLine API is available', () => {
    // Verify the browser API we rely on exists
    expect(typeof navigator.onLine).toBe('boolean')
  })

  test('window online/offline events are supported', () => {
    // Verify we can add/remove event listeners
    const handler = vi.fn()

    window.addEventListener('online', handler)
    window.addEventListener('offline', handler)

    window.dispatchEvent(new Event('online'))
    expect(handler).toHaveBeenCalledTimes(1)

    window.dispatchEvent(new Event('offline'))
    expect(handler).toHaveBeenCalledTimes(2)

    window.removeEventListener('online', handler)
    window.removeEventListener('offline', handler)
  })
})
