/**
 * MicButton component - Compact microphone button for voice input
 * 36px button with color-based status indication (no reactor rings)
 */

export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

interface MicButtonProps {
  status: VoiceStatus
  disabled?: boolean
  onConnect: () => void
  onPressStart: () => void
  onPressEnd: () => void
}

export function MicButton({
  status,
  disabled = false,
  onConnect,
  onPressStart,
  onPressEnd,
}: MicButtonProps) {
  const isConnected = status !== 'idle' && status !== 'connecting' && status !== 'error'
  const isConnecting = status === 'connecting'

  const handleClick = () => {
    if (status === 'idle' || status === 'error') {
      onConnect()
    }
  }

  const handlePressStart = () => {
    if (isConnected && !disabled) {
      onPressStart()
    }
  }

  const handlePressEnd = () => {
    if (isConnected && !disabled) {
      onPressEnd()
    }
  }

  const getAriaLabel = () => {
    if (status === 'idle') return 'Connect to voice'
    if (status === 'error') return 'Retry voice connection'
    if (status === 'connecting') return 'Connecting...'
    return 'Hold to talk'
  }

  return (
    <button
      type="button"
      className={`mic-button mic-button--${status} ${disabled ? 'mic-button--disabled' : ''}`}
      aria-label={getAriaLabel()}
      disabled={isConnecting || disabled}
      onClick={handleClick}
      onMouseDown={handlePressStart}
      onMouseUp={handlePressEnd}
      onMouseLeave={handlePressEnd}
      onTouchStart={handlePressStart}
      onTouchEnd={handlePressEnd}
    >
      <svg
        className="mic-button__icon"
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
  )
}
