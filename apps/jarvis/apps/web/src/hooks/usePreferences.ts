/**
 * usePreferences hook - Manages chat model and reasoning preferences
 *
 * Listens to stateManager events and syncs with React context.
 * Provides methods to update preferences (which sync to server).
 */

import { useEffect, useCallback } from 'react'
import { useAppState, useAppDispatch } from '../context'
import { stateManager, type ChatPreferences, type ModelInfo } from '../../lib/state-manager'
import { CONFIG, toAbsoluteUrl } from '../../lib/config'

export function usePreferences() {
  const state = useAppState()
  const dispatch = useAppDispatch()

  // Listen for preference changes from stateManager
  useEffect(() => {
    const handleStateChange = (event: { type: string; preferences?: ChatPreferences; models?: ModelInfo[] }) => {
      if (event.type === 'PREFERENCES_CHANGED' && event.preferences) {
        dispatch({ type: 'SET_PREFERENCES', preferences: event.preferences })
      }
      if (event.type === 'MODELS_LOADED' && event.models) {
        dispatch({ type: 'SET_AVAILABLE_MODELS', models: event.models })
      }
    }

    stateManager.addListener(handleStateChange)

    // Seed initial state from the current bootstrap (avoids race where bootstrap
    // loads before this hook subscribes to stateManager events).
    const bootstrap = stateManager.getBootstrap()
    if (bootstrap?.available_models) {
      dispatch({ type: 'SET_AVAILABLE_MODELS', models: bootstrap.available_models })
    }
    dispatch({ type: 'SET_PREFERENCES', preferences: stateManager.getPreferences() })

    return () => stateManager.removeListener(handleStateChange)
  }, [dispatch])

  // Update a single preference and sync to server
  const updatePreference = useCallback(
    async (key: keyof ChatPreferences, value: string) => {
      // Update local state immediately for responsiveness
      dispatch({ type: 'UPDATE_PREFERENCE', key, value })
      stateManager.updatePreferences({ [key]: value })

      // Persist to server
      try {
        const response = await fetch(toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/preferences`), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ [key]: value }),
        })

        if (!response.ok) {
          console.error('[usePreferences] Failed to save preference:', response.statusText)
          // Could revert here, but for now just log
        }
      } catch (error) {
        console.error('[usePreferences] Error saving preference:', error)
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
    (effort: string) => updatePreference('reasoning_effort', effort),
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
