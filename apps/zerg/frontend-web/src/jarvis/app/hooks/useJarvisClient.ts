/**
 * useJarvisClient hook - Zerg backend communication
 *
 * This hook manages the connection to the Zerg backend via JarvisClient.
 * Handles authentication, session management, and fiche communication.
 */

import { useCallback, useEffect, useRef } from 'react'
import { useAppState, useAppDispatch } from '../context'
import { getJarvisClient } from '../../core'

// Get API URL from environment or default
function getZergApiUrl(): string {
  // Check for environment variable (Vite style)
  if (typeof import.meta !== 'undefined' && (import.meta as unknown as { env?: Record<string, string> }).env) {
    const env = (import.meta as unknown as { env: Record<string, string> }).env
    if (env.VITE_API_URL) return env.VITE_API_URL
  }

  // Default based on location
  if (typeof window !== 'undefined') {
    // In production, use same origin
    if (window.location.hostname !== 'localhost') {
      return `${window.location.origin}/api`
    }
  }

  // Development default
  return 'http://localhost:47300'
}

export interface UseJarvisClientOptions {
  autoConnect?: boolean
  onConnected?: () => void
  onDisconnected?: () => void
  onError?: (error: Error) => void
}

export function useJarvisClient(options: UseJarvisClientOptions = {}) {
  const state = useAppState()
  const dispatch = useAppDispatch()
  const clientRef = useRef<ReturnType<typeof getJarvisClient> | null>(null)

  const { jarvisClient, isConnected, cachedFiches } = state

  // Initialize client
  const initialize = useCallback(async () => {
    try {
      const apiUrl = getZergApiUrl()
      console.log('[useJarvisClient] Initializing with URL:', apiUrl)

      const client = getJarvisClient(apiUrl)
      clientRef.current = client
      dispatch({ type: 'SET_JARVIS_CLIENT', client })

      // Check if already authenticated (async since it calls /api/auth/verify)
      const isAuthed = await client.isAuthenticated()
      if (isAuthed) {
        console.log('[useJarvisClient] Already authenticated')
      }

      return client
    } catch (error) {
      console.error('[useJarvisClient] Initialization failed:', error)
      options.onError?.(error as Error)
      return null
    }
  }, [dispatch, options])

  // Connect to Zerg backend (SSE event stream)
  const connect = useCallback(async () => {
    if (!clientRef.current) {
      await initialize()
    }

    if (!clientRef.current) {
      console.warn('[useJarvisClient] Client not initialized')
      return
    }

    clientRef.current.connectEventStream({
      onConnected: () => {
        dispatch({ type: 'SET_CONNECTED', connected: true })
        options.onConnected?.()
      },
      onError: (error) => {
        dispatch({ type: 'SET_CONNECTED', connected: false })
        options.onError?.(new Error('Jarvis event stream error'))
        console.error('[useJarvisClient] Event stream error:', error)
      },
    })
  }, [dispatch, initialize, options])

  // Disconnect
  const disconnect = useCallback(() => {
    clientRef.current?.disconnectEventStream()
    dispatch({ type: 'SET_CONNECTED', connected: false })
    options.onDisconnected?.()
  }, [dispatch, options])

  // Fetch available fiches
  const fetchFiches = useCallback(async () => {
    if (!clientRef.current) {
      console.warn('[useJarvisClient] Client not initialized')
      return []
    }

    try {
      const fiches = await clientRef.current.listFiches()
      dispatch({ type: 'SET_CACHED_FICHES', fiches })
      return fiches
    } catch (error) {
      console.error('[useJarvisClient] Failed to fetch fiches:', error)
      options.onError?.(error as Error)
      return []
    }
  }, [dispatch, options])

  // Auto-connect on mount if enabled
  useEffect(() => {
    if (options.autoConnect !== false) {
      initialize()
    }
  }, [initialize, options.autoConnect])

  return {
    // State
    client: jarvisClient,
    isConnected,
    fiches: cachedFiches,

    // Actions
    initialize,
    connect,
    disconnect,
    fetchFiches,
  }
}
