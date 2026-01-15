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

export interface ModelCapabilities {
  reasoning?: boolean
  reasoningNone?: boolean
}

export interface ModelInfo {
  id: string
  display_name: string
  description: string
  capabilities?: ModelCapabilities
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
    (model: string) => {
      updatePreference('chat_model', model)

      // Find the new model's capabilities
      const newModel = state.availableModels?.find((m) => m.id === model)
      const supportsReasoning = newModel?.capabilities?.reasoning ?? false
      const supportsReasoningNone = newModel?.capabilities?.reasoningNone ?? false

      // Reset reasoning_effort if model doesn't support current setting
      if (!supportsReasoning) {
        // Model doesn't support reasoning at all - reset to 'none' (will be ignored)
        updatePreference('reasoning_effort', 'none')
      } else if (!supportsReasoningNone && state.preferences.reasoning_effort === 'none') {
        // Model doesn't support 'none' but user has 'none' selected - reset to 'low'
        updatePreference('reasoning_effort', 'low')
      }
    },
    [updatePreference, state.availableModels, state.preferences.reasoning_effort]
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
