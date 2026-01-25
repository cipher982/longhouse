/**
 * TextInput component - Text message input with integrated mic button
 */

import { useState, useCallback, KeyboardEvent } from 'react'
import { MicButton, type VoiceStatus } from './MicButton'

interface TextInputProps {
  onSend: (message: string) => void
  disabled?: boolean
  placeholder?: string
  // Voice control props
  micStatus?: VoiceStatus
  onMicConnect?: () => void
  onMicPressStart?: () => void
  onMicPressEnd?: () => void
}

export function TextInput({
  onSend,
  disabled = false,
  placeholder = 'Type a message...',
  micStatus = 'idle',
  onMicConnect,
  onMicPressStart,
  onMicPressEnd,
}: TextInputProps) {
  const [value, setValue] = useState('')

  const getVoiceHint = useCallback(() => {
    switch (micStatus) {
      case 'idle':
        return 'Tap mic to enable voice'
      case 'connecting':
        return 'Connecting...'
      case 'ready':
        return 'Tap to talk'
      case 'listening':
        return 'Tap to stop'
      case 'processing':
        return 'Processing...'
      case 'speaking':
        return 'Speaking...'
      case 'error':
        return 'Tap to retry'
      default:
        return ''
    }
  }, [micStatus])

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
      {onMicConnect && (
        <div className="mic-button-stack">
          <MicButton
            status={micStatus}
            disabled={disabled}
            onConnect={onMicConnect}
            onPressStart={onMicPressStart || (() => {})}
            onPressEnd={onMicPressEnd || (() => {})}
          />
          <span className="mic-button-hint" aria-live="polite">
            {getVoiceHint()}
          </span>
        </div>
      )}
      <input
        type="text"
        className="text-input"
        data-testid="chat-input"
        placeholder={placeholder}
        aria-label="Message input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
      />
      <button
        className="send-button"
        type="button"
        data-testid="send-message-btn"
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
