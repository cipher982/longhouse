/**
 * MicButton component - Hold-to-talk microphone control with visualizer rings
 */

import type { CSSProperties } from 'react'

export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

const EQ_BAR_FACTORS = [0.25, 0.4, 0.55, 0.75, 1, 0.85, 0.65, 0.45, 0.3, 0.5, 0.7, 0.9]

interface MicButtonProps {
  status: VoiceStatus
  disabled?: boolean
  level?: number
  onConnect: () => void
  onPressStart: () => void
  onPressEnd: () => void
}

export function MicButton({
  status,
  disabled = false,
  level = 0,
  onConnect,
  onPressStart,
  onPressEnd,
}: MicButtonProps) {
  const isConnected = status !== 'idle' && status !== 'connecting' && status !== 'error'
  const isConnecting = status === 'connecting'
  const isBusy = status === 'processing' || status === 'speaking'
  const isDisabled = disabled || isConnecting || isBusy
  const normalizedLevel = Math.max(0, Math.min(1, level))

  const handleClick = () => {
    if (status === 'idle' || status === 'error') {
      onConnect()
    }
  }

  const handlePressStart = () => {
    if (isConnected && !isDisabled) {
      onPressStart()
    }
  }

  const handlePressEnd = () => {
    if (isConnected && !isDisabled) {
      onPressEnd()
    }
  }

  const getAriaLabel = () => {
    if (status === 'idle') return 'Connect to voice'
    if (status === 'error') return 'Retry voice connection'
    if (status === 'connecting') return 'Connecting...'
    if (status === 'processing') return 'Processing...'
    if (status === 'speaking') return 'Speaking...'
    if (status === 'listening') return 'Release to send'
    return 'Hold to talk'
  }

  return (
    <div
      className={`voice-button-wrapper compact ${status}`}
      style={{ "--mic-level": normalizedLevel } as CSSProperties}
    >
      <div className="mic-eq-ring" aria-hidden="true">
        {EQ_BAR_FACTORS.map((factor, index) => (
          <span
            key={`eq-${index}`}
            className="mic-eq-bar"
            style={{
              "--bar-angle": `${index * (360 / EQ_BAR_FACTORS.length)}deg`,
              "--bar-factor": String(factor),
            } as CSSProperties}
          />
        ))}
      </div>
      <button
        type="button"
        className={`voice-button mic-button ${status} ${isDisabled ? 'disabled' : ''}`}
        aria-label={getAriaLabel()}
        disabled={isDisabled}
        onClick={handleClick}
        onMouseDown={handlePressStart}
        onMouseUp={handlePressEnd}
        onMouseLeave={handlePressEnd}
        onTouchStart={handlePressStart}
        onTouchEnd={handlePressEnd}
      >
        <svg
          className="voice-icon"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
          <path d="M19 10v2a7 7 0 01-14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="23" />
          <line x1="8" y1="23" x2="16" y2="23" />
        </svg>
      </button>
    </div>
  )
}
