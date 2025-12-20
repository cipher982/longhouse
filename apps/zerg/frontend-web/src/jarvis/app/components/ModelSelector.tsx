/**
 * ModelSelector component - Dropdown selectors for model and reasoning effort
 */

import { usePreferences } from '../hooks'

const REASONING_OPTIONS = [
  { value: 'none', label: 'None' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]

export function ModelSelector() {
  const { availableModels, preferences, setModel, setReasoningEffort } = usePreferences()

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

      <select
        className="reasoning-select"
        value={preferences.reasoning_effort}
        onChange={(e) => setReasoningEffort(e.target.value)}
        aria-label="Reasoning effort"
      >
        {REASONING_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  )
}
