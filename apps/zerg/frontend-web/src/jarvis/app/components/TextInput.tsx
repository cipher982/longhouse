/**
 * TextInput component - Text message input
 */

import { useState, useCallback, KeyboardEvent } from 'react'

interface TextInputProps {
  onSend: (message: string) => void
  disabled?: boolean
  placeholder?: string
}

export function TextInput({ onSend, disabled = false, placeholder = 'Type a message...' }: TextInputProps) {
  const [value, setValue] = useState('')

  const handleSend = useCallback(() => {
    const trimmed = value.trim()
    if (trimmed && !disabled) {
      onSend(trimmed)
      setValue('')
    }
  }, [value, disabled, onSend])

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSend()
      }
    },
    [handleSend]
  )

  return (
    <div className="text-input-container">
      <input
        type="text"
        className="text-input"
        placeholder={placeholder}
        aria-label="Message input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
      />
      <button
        className="send-button"
        type="button"
        aria-label="Send message"
        onClick={handleSend}
        disabled={disabled || !value.trim()}
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
      </button>
    </div>
  )
}
