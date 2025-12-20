/**
 * usePreferences hook - Manages chat model and reasoning preferences
 *
 * Reads from React context (populated by useJarvisApp during initialization).
 * Provides methods to update preferences (which sync to server).
 */

import { useCallback } from 'react'
import { useAppState, useAppDispatch } from '../context'
import { CONFIG, toAbsoluteUrl } from '../../lib/config'
import { logger } from '../../core'

export interface ChatPreferences {
  chat_model: string
  reasoning_effort: 'none' | 'low' | 'medium' | 'high'
}

export interface ModelInfo {
  id: string
  display_name: string
  description: string
}

export function usePreferences() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Update a single preference and sync to server
  const updatePreference = useCallback(
    async (key: keyof ChatPreferences, value: string) => {
      // Update local state immediately for responsiveness
      dispatch({ type: 'UPDATE_PREFERENCE', key, value })

      // Persist to server
      try {
        const response = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/preferences`), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ [key]: value }),
        })

        if (!response.ok) {
          logger.error('[usePreferences] Failed to save preference:', response.statusText)
        }
      } catch (error) {
        logger.error('[usePreferences] Error saving preference:', error)
      }
    },
    [dispatch]
  )

  // Convenience methods
  const setModel = useCallback(
    (model: string) => updatePreference('chat_model', model),
    [updatePreference]
  )

  const setReasoningEffort = useCallback(
    (effort: string) => updatePreference('reasoning_effort', effort as ChatPreferences['reasoning_effort']),
    [updatePreference]
  )

  return {
    availableModels: state.availableModels,
    preferences: state.preferences,
    updatePreference,
    setModel,
    setReasoningEffort,
  }
}
