/**
 * ModelSelector component - Dropdown selectors for model and reasoning effort
 */

import { useMemo } from 'react'
import { usePreferences } from '../hooks'

const ALL_REASONING_OPTIONS = [
  { value: 'none', label: 'None' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]

export function ModelSelector() {
  const { availableModels, preferences, setModel, setReasoningEffort } = usePreferences()

  // Find selected model's capabilities
  const selectedModel = useMemo(
    () => availableModels?.find((m) => m.id === preferences.chat_model),
    [availableModels, preferences.chat_model]
  )

  const supportsReasoning = selectedModel?.capabilities?.reasoning ?? false
  const supportsReasoningNone = selectedModel?.capabilities?.reasoningNone ?? false

  // Filter reasoning options based on model capabilities
  const reasoningOptions = useMemo(() => {
    if (!supportsReasoning) return []
    return supportsReasoningNone
      ? ALL_REASONING_OPTIONS
      : ALL_REASONING_OPTIONS.filter((opt) => opt.value !== 'none')
  }, [supportsReasoning, supportsReasoningNone])

  // Don't render until models are loaded
  if (!availableModels || availableModels.length === 0) {
    return null
  }

  return (
    <div className="model-selector">
      <select
        className="model-select"
        value={preferences.chat_model}
        onChange={(e) => setModel(e.target.value)}
        aria-label="Select model"
      >
        {availableModels.map((model) => (
          <option key={model.id} value={model.id}>
            {model.display_name}
          </option>
        ))}
      </select>

      {supportsReasoning && (
        <select
          className="reasoning-select"
          value={preferences.reasoning_effort}
          onChange={(e) => setReasoningEffort(e.target.value)}
          aria-label="Reasoning effort"
        >
          {reasoningOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )}
    </div>
  )
}
