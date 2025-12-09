/**
 * VoiceControls component - Push-to-talk and hands-free mode
 */

export type VoiceMode = 'push-to-talk' | 'hands-free'
export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'

interface VoiceControlsProps {
  mode: VoiceMode
  status: VoiceStatus
  disabled?: boolean
  onModeToggle: () => void
  onVoiceButtonPress: () => void
  onVoiceButtonRelease: () => void
  onConnect?: () => void
}

const STATUS_TEXT: Record<VoiceStatus, string> = {
  idle: 'Tap to connect',
  connecting: 'Connecting...',
  ready: 'Ready - hold to talk',
  listening: 'Listening...',
  processing: 'Processing...',
  speaking: 'Speaking...',
  error: 'Connection failed - tap to retry',
}

export function VoiceControls({
  mode,
  status,
  disabled = false,
  onModeToggle,
  onVoiceButtonPress,
  onVoiceButtonRelease,
  onConnect,
}: VoiceControlsProps) {
  const isHandsFree = mode === 'hands-free'
  const isConnected = status !== 'idle' && status !== 'connecting' && status !== 'error'
  const isConnecting = status === 'connecting'

  // Handle button click for non-connected states
  const handleButtonClick = () => {
    if (status === 'idle' || status === 'error') {
      onConnect?.()
    }
  }

  // Only allow PTT in connected state
  const handlePress = () => {
    if (isConnected && !disabled) {
      onVoiceButtonPress()
    }
  }

  const handleRelease = () => {
    if (isConnected && !disabled) {
      onVoiceButtonRelease()
    }
  }

  return (
    <div className="voice-controls">
      {/* Mode Toggle Switch */}
      <div className="mode-toggle-container">
        <span className="mode-label">Push-to-Talk</span>
        <button
          className={`mode-toggle ${isHandsFree ? 'active' : ''}`}
          type="button"
          role="switch"
          aria-checked={isHandsFree}
          aria-label="Toggle hands-free mode"
          onClick={onModeToggle}
          disabled={!isConnected || disabled}
        >
          <span className="mode-toggle-slider"></span>
        </button>
        <span className="mode-label">Hands-free</span>
      </div>

      {/* Main Voice Button */}
      <div className="voice-button-wrapper">
        <button
          id="pttBtn"
          className={`voice-button ${status} ${disabled ? 'disabled' : ''}`}
          type="button"
          aria-label={isConnected ? 'Press to talk' : 'Connect to voice'}
          disabled={isConnecting || disabled}
          onClick={handleButtonClick}
          onMouseDown={handlePress}
          onMouseUp={handleRelease}
          onMouseLeave={handleRelease}
          onTouchStart={handlePress}
          onTouchEnd={handleRelease}
        >
          <div className="voice-button-ring"></div>
          <svg className="voice-icon" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
            <path d="M19 10v2a7 7 0 01-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="23" />
            <line x1="8" y1="23" x2="16" y2="23" />
          </svg>
        </button>
      </div>

      {/* Status Text */}
      <div className="voice-status">
        <span className="voice-status-text" aria-live="polite">
          {STATUS_TEXT[status]}
        </span>
      </div>
    </div>
  )
}
